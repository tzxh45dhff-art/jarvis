"""
Azure OpenAI backend - GPT-6 Luna on the user's Azure for Students credits.

Measured from Azure Cloud Shell (October 2026), three runs each:

  chat/completions, reasoning off   0.9-1.2 s   set_brightness("100")
  responses API, effort low         1.9-2.5 s   set_brightness("max")

Two facts shape this file:

* **No API key is stored anywhere.** The user asked for that explicitly. Auth
  is Microsoft Entra ID: a bearer token from the Azure CLI (`az login` once),
  cached in memory until shortly before it expires and then fetched again.
  The resource only needs the "Cognitive Services OpenAI User" role.
* **GPT-6 Luna refuses tools on chat/completions unless reasoning is off**
  ("Function tools with reasoning_effort are not supported ... set
  reasoning_effort to 'none'"). Tool turns therefore send
  reasoning_effort="none" - which is also the fast path.

Endpoint and deployment name are not secrets; they live in
~/.jarvis/azure.json.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Iterator

from ..config import CONFIG, JARVIS_HOME
from . import protocol as P
from . import http_pool
from . import ledger
from .llm import Completion, Delta

log = logging.getLogger("jarvis.azure")

SETTINGS = JARVIS_HOME / "azure.json"
SCOPE = "https://cognitiveservices.azure.com"
_RETRYABLE = {429, 500, 502, 503, 504}
# Deployments found (by a 400 from the service) to reject reasoning_effort=none.
_NO_NONE: set[str] = set()


# azure.json keys that can also be set from the environment (or a .env file).
# The environment wins. "off" / "none" / "false" clears an optional route.
_ENV_SETTINGS = {
    "endpoint": "JARVIS_AZURE_ENDPOINT",
    "deployment": "JARVIS_AZURE_DEPLOYMENT",             # the main model
    "fast_deployment": "JARVIS_AZURE_FAST_DEPLOYMENT",   # short single-step requests
    "web_deployment": "JARVIS_AZURE_WEB_DEPLOYMENT",     # plain web lookups
    "search_deployment": "JARVIS_AZURE_SEARCH_DEPLOYMENT",  # the grounded-search call itself
    "background_deployment": "JARVIS_AZURE_BACKGROUND_DEPLOYMENT",  # learning from chat
    "blender_deployment": "JARVIS_AZURE_BLENDER_DEPLOYMENT",  # 3D work
    "cache_retention": "JARVIS_AZURE_CACHE_RETENTION",   # "24h" keeps the prompt cache warm
    "api": "JARVIS_AZURE_API",                           # "responses" (default) or "chat"
}


def settings() -> dict:
    """~/.jarvis/azure.json, overridden by JARVIS_AZURE_* environment variables."""
    try:
        s = json.loads(SETTINGS.read_text())
    except Exception:
        s = {}
    for key, env in _ENV_SETTINGS.items():
        v = (os.environ.get(env) or "").strip()
        if v:
            s[key] = "" if v.lower() in ("off", "none", "false") else v
    return s


def _pause(seconds: float, stop_event) -> bool:
    """Wait before a retry, waking at once if the user stops the request.
    True when stopped - the caller then gives up instead of calling again."""
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


def _stopped(stop_event) -> bool:
    return stop_event is not None and stop_event.is_set()


def _az() -> str | None:
    for p in ("/opt/homebrew/bin/az", "/usr/local/bin/az"):
        if os.path.exists(p):
            return p
    return None


class _Token:
    """Entra ID bearer token, refreshed before it expires. Never written to disk."""

    def __init__(self):
        self._tok, self._exp = "", 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._tok and time.time() < self._exp - 300:
                return self._tok
            az = _az()
            if not az:
                raise RuntimeError("Azure CLI is not installed (brew install azure-cli)")
            r = subprocess.run([az, "account", "get-access-token", "--resource", SCOPE,
                                "-o", "json"], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError("Azure sign-in needed - run `az login` once ("
                                   + (r.stderr or "").strip()[:160] + ")")
            d = json.loads(r.stdout)
            self._tok = d["accessToken"]
            self._exp = float(d.get("expires_on") or time.time() + 3000)
            return self._tok


_TOKEN = _Token()
_BACKGROUND: dict = {}
_CACHE_OK: dict[str, bool] = {}


def _api() -> str:
    """'responses' (default) or 'chat' - which Azure API carries agent steps."""
    return settings().get("api") or "responses"


def _cache_params(model: str) -> dict:
    """Prompt-cache routing for every request. A fixed key sends all of
    JARVIS's requests to the same cache; `cache_retention` in azure.json
    ("24h") keeps the cached prompt alive across idle gaps, so the first
    request after a break reads the cache (0.1x input price) instead of
    re-storing it (1.25x)."""
    if not _CACHE_OK.get(model, True) or os.environ.get("JARVIS_CACHE_PARAMS") == "0":
        return {}
    out = {"prompt_cache_key": "jarvis-" + model}
    ret = settings().get("cache_retention")
    if ret:
        out["prompt_cache_retention"] = ret
    return out


def background():
    """A separate client for background chores (learning facts from the chat):
    small JSON-extraction jobs that GPT-6 Luna does well at ~1/20 of Sol's
    price. Separate so it never borrows the agent's per-turn model (a Blender
    turn runs on Astra, 5x Sol)."""
    cfg = settings()
    dep = cfg.get("background_deployment") or cfg.get("deployment", "")
    if dep not in _BACKGROUND:
        _BACKGROUND[dep] = AzureBackend(deployment=dep)
    return _BACKGROUND[dep]


class _StripCiteMarkers:
    """Removes citation markers some models append to a reply: an invisible
    private-use character, "cite", "turn0search0"-style ids, another one - which
    arrives in the terminal as the literal text 'citeturn0search0'. Stateful, so
    a marker split across streamed chunks is still removed."""

    def __init__(self) -> None:
        self.inside = False

    def feed(self, text: str) -> str:
        out = []
        for ch in text:
            if self.inside:
                if ch == "\ue201":
                    self.inside = False
                continue
            if ch == "\ue200":
                self.inside = True
            elif "\ue000" <= ch <= "\uf8ff":
                continue
            else:
                out.append(ch)
        return "".join(out)


class AzureBackend:
    """Drop-in replacement for the other backends."""

    def __init__(self, endpoint: str = "", deployment: str = ""):
        s = settings()
        self.endpoint = (endpoint or s.get("endpoint", "")).rstrip("/") + "/"
        self.model_id = deployment or s.get("deployment", "")
        self._usage = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "requests": 0}
        self._by_model: dict[str, dict] = {}
        # Set per request by the router: a cheaper deployment for simple turns.
        self.turn_model: str | None = None
        self._checked = False

    @property
    def available(self) -> bool:
        return bool(self.endpoint.strip("/") and self.model_id and _az())

    ready = available

    def wait_ready(self, timeout=None) -> bool:
        return self.available

    # -- translation -------------------------------------------------------
    @staticmethod
    def _to_messages(messages: list[dict]) -> list[dict]:
        """
        Agent messages -> OpenAI chat messages.

        OpenAI rejects a tool result without its call and a call without its
        result. Trimming can produce either; both are repaired.
        """
        out: list[dict] = []
        open_ids: list[str] = []

        def close_open(reason: str):
            for i in open_ids:
                out.append({"role": "tool", "tool_call_id": i, "content": reason})
            open_ids.clear()

        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "system":
                out.append({"role": "system", "content": content})
            elif role == "tool":
                tid = m.get("tool_call_id") or ""
                if tid in open_ids:
                    open_ids.remove(tid)
                    out.append({"role": "tool", "tool_call_id": tid,
                                "content": str(content)[:16000] or "(no output)"})
                else:
                    out.append({"role": "user", "content":
                                f"[earlier result from {m.get('name') or 'tool'}] "
                                f"{str(content)[:4000]}"})
            elif role == "assistant":
                # Calls left open by an earlier assistant turn are answered
                # before this one speaks: the API rejects the history otherwise.
                close_open("(no result: the step was interrupted)")
                calls = []
                for i, tc in enumerate(m.get("tool_calls") or []):
                    fn = tc.get("function", tc)
                    args = fn.get("arguments") or {}
                    tid = tc.get("id") or f"call_{len(out)}_{i}"
                    calls.append({"id": tid, "type": "function", "function": {
                        "name": fn.get("name", ""),
                        "arguments": args if isinstance(args, str) else json.dumps(args)}})
                    open_ids.append(tid)
                msg = {"role": "assistant", "content": str(content) or None}
                if calls:
                    msg["tool_calls"] = calls
                if msg["content"] or calls:
                    out.append(msg)
            else:
                close_open("(no result: the step was interrupted)")
                out.append({"role": "user", "content": str(content)})
        close_open("(no result recorded)")
        return out

    @staticmethod
    def _to_tools(tools):
        if not tools:
            return None
        return [{"type": "function", "function": t.get("function", t)} for t in tools]

    # -- http --------------------------------------------------------------
    def _post(self, body: dict, stream: bool = True):
        req = urllib.request.Request(
            self.endpoint + "openai/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + _TOKEN.get()})
        return http_pool.urlopen(req, timeout=120)

    # -- generation --------------------------------------------------------
    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None) -> Iterator[Delta]:
        # Every step goes through the Responses API, reasoning off unless asked.
        # The two APIs keep separate prompt caches (measured 19 Oct 2026): a
        # turn that thought on its first step (Responses) and acted on the next
        # (chat) stored the whole 14k+ token prompt twice, at 1.25x the input
        # price each time. On one API the cache is shared, and switching the
        # reasoning effort between steps keeps it (25 tokens re-stored).
        # chat/completions stays as the fallback.
        if think or self._model() in _NO_NONE or _api() == "responses":
            produced_any = False
            try:
                for d in self._stream_responses(messages, tools, max_tokens, stop_event,
                                                think=think):
                    if d.kind != "done":
                        produced_any = True
                    yield d
                if produced_any:
                    return
                log.warning("responses API gave nothing; using chat completions")
            except Exception as e:
                if produced_any:            # text already shown - don't repeat it
                    yield Delta(kind="done")
                    return
                log.warning("responses API failed (%s); using chat completions", e)
            if _stopped(stop_event):
                # The user stopped it: "gave nothing" because it was cut off,
                # not because the route failed. No second, paid request.
                yield Delta(kind="done")
                return
        body = {"model": self._model(), "messages": self._to_messages(messages),
                "stream": True, "stream_options": {"include_usage": True},
                "max_completion_tokens": max(max_tokens or 0, 16384)
                if not max_tokens or max_tokens >= CONFIG.llm.max_tokens else max_tokens}
        t = self._to_tools(tools)
        if t:
            body["tools"] = t
            body["reasoning_effort"] = "none"     # required for tools, and fastest
        else:
            body["reasoning_effort"] = "none"
        body.update(_cache_params(body["model"]))
        last = ""
        for attempt in range(4):
            produced = [False]
            try:
                yield from self._stream_once(body, stop_event, produced)
                return
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                try:
                    last = json.loads(raw)["error"]["message"]
                except Exception:
                    last = raw[:200] or f"HTTP {e.code}"
                if e.code == 400 and "reasoning_effort" in last and "'none'" in last \
                        and self._model() not in _NO_NONE:
                    # This model (GPT-6 Astra, 6.1 Sol...) cannot run with
                    # reasoning off. Remember that, and take the route that
                    # allows tools together with reasoning.
                    _NO_NONE.add(self._model())
                    log.info("%s does not support reasoning_effort=none; using "
                             "the Responses API", self.model_id)
                    yield from self.stream(messages, tools, True, max_tokens,
                                           temperature, stop_event)
                    return
                if e.code in (401, 403):
                    yield Delta(kind="text", text=f"(Azure refused access: {last[:200]})")
                    yield Delta(kind="done")
                    return
                if e.code in _RETRYABLE and attempt < 3 and not produced[0]:
                    # A rate limit resets on a one-minute window; Azure says how
                    # long to wait. Retrying after 0.8-3 s just burned the
                    # retries and gave up mid-task.
                    wait = 0.8 * (2 ** attempt)
                    if e.code == 429:
                        h = e.headers or {}
                        try:
                            wait = float(h.get("retry-after-ms") or 0) / 1000 or \
                                float(h.get("retry-after") or 0) or 10.0
                        except ValueError:
                            wait = 10.0
                        wait = min(max(wait, 2.0), 30.0)
                        log.warning("azure rate limit; waiting %.0fs", wait)
                    if _pause(wait, stop_event):
                        yield Delta(kind="done")
                        return
                    continue
                log.error("azure error %s: %s", e.code, last)
                yield Delta(kind="text", text=f"(Azure: {last[:240]})")
                yield Delta(kind="done")
                return
            except Exception as e:
                last = str(e)
                if _stopped(stop_event):
                    yield Delta(kind="done")
                    return
                if not produced[0] and attempt < 3:
                    if _pause(0.8 * (attempt + 1), stop_event):
                        yield Delta(kind="done")
                        return
                    continue
                yield Delta(kind="text", text=f"(Azure request failed: {last[:200]})")
                yield Delta(kind="done")
                return

    def _stream_once(self, body, stop_event, produced) -> Iterator[Delta]:
        resp = self._post(body)
        self._add("requests", 1)
        calls: dict[int, dict] = {}
        n_out, first = 0, None
        strip = _StripCiteMarkers()
        try:
            for raw in resp:
                if stop_event is not None and stop_event.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ch = json.loads(data)
                except ValueError:
                    continue
                u = ch.get("usage")
                if u:
                    n_out = int(u.get("completion_tokens", 0) or 0)
                    det = u.get("prompt_tokens_details") or {}
                    p_in = int(u.get("prompt_tokens", 0) or 0)
                    p_rd = int(det.get("cached_tokens", 0) or 0)
                    # Billed at 1.25x input: storing the prefix in the cache.
                    p_wr = int(det.get("cache_write_tokens", 0) or 0)
                    self._add("in", p_in)
                    self._add("cache_read", p_rd)
                    self._add("cache_write", p_wr)
                    ledger.record(body.get("model", self._model()), "step-chat", p_in, p_rd, p_wr, n_out)
                for c in ch.get("choices") or []:
                    d = c.get("delta") or {}
                    if d.get("content"):
                        first = first or time.time()
                        produced[0] = True
                        piece = strip.feed(d["content"])
                        if piece:
                            yield Delta(kind="text", text=piece)
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["args"] += fn.get("arguments") or ""
        finally:
            try:
                resp.close()
            except Exception:
                pass
        for i in sorted(calls):
            c = calls[i]
            try:
                args = json.loads(c["args"] or "{}")
            except ValueError:
                args = {}
            produced[0] = True
            yield Delta(kind="tool_call", tool_call=P.ToolCall(
                name=c["name"], arguments=args if isinstance(args, dict) else {"input": args},
                id=c["id"] or f"call_{time.time_ns()}_{i}"))
        self._add("out", n_out)
        el = time.time() - first if first else 0
        yield Delta(kind="done", n_tokens=n_out, tps=n_out / el if el > 0.05 else 0.0)

    # -- reasoning path ----------------------------------------------------
    #
    # Chat completions refuses tools unless reasoning is off, so hard,
    # multi-step turns go through the Responses API, which allows both.
    # Measured: ~2 s per step instead of ~1 s - spent only when the agent asks
    # for deep reasoning.

    @classmethod
    def _to_input(cls, messages: list[dict]) -> tuple[list[dict], str]:
        """Chat-style messages -> Responses API input items + instructions."""
        items: list[dict] = []
        system = ""
        for m in cls._to_messages(messages):
            role = m["role"]
            if role == "system":
                system = m["content"]
            elif role == "tool":
                items.append({"type": "function_call_output",
                              "call_id": m["tool_call_id"],
                              "output": str(m.get("content") or "")})
            elif role == "assistant":
                if m.get("content"):
                    items.append({"role": "assistant", "content": m["content"]})
                for tc in m.get("tool_calls") or []:
                    items.append({"type": "function_call", "call_id": tc["id"],
                                  "name": tc["function"]["name"],
                                  "arguments": tc["function"]["arguments"]})
            else:
                items.append({"role": "user", "content": m.get("content") or ""})
        return items, system

    def _stream_responses(self, messages, tools, max_tokens, stop_event,
                          think: bool = True) -> Iterator[Delta]:
        items, system = self._to_input(messages)
        model = self._model()
        effort = "low" if (think or model in _NO_NONE) else "none"
        body = {"model": model, "input": items, "stream": True,
                "reasoning": {"effort": effort}, "store": False,
                "max_output_tokens": max(max_tokens or 0, 16384)
                if not max_tokens or max_tokens >= CONFIG.llm.max_tokens else max_tokens}
        if system:
            body["instructions"] = system
        if tools:
            body["tools"] = [{"type": "function", **t.get("function", t)} for t in tools]
        body.update(_cache_params(model))
        resp = None
        for attempt in range(4):
            req = urllib.request.Request(
                self.endpoint + "openai/v1/responses", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + _TOKEN.get()})
            try:
                resp = http_pool.urlopen(req, timeout=180)
                break
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                if e.code == 400 and "effort" in raw and body["reasoning"]["effort"] == "none":
                    _NO_NONE.add(model)                 # this model must reason
                    body["reasoning"] = {"effort": "low"}
                    continue
                if e.code == 400 and "prompt_cache" in raw and _CACHE_OK.get(model, True):
                    _CACHE_OK[model] = False            # the parameter is not accepted here
                    for k in ("prompt_cache_key", "prompt_cache_retention"):
                        body.pop(k, None)
                    continue
                if e.code in _RETRYABLE and attempt < 3:
                    wait = 0.8 * (2 ** attempt)
                    if e.code == 429:
                        h = e.headers or {}
                        try:
                            wait = float(h.get("retry-after-ms") or 0) / 1000 or \
                                float(h.get("retry-after") or 0) or 10.0
                        except ValueError:
                            wait = 10.0
                        wait = min(max(wait, 2.0), 30.0)
                        log.warning("azure rate limit; waiting %.0fs", wait)
                    if _pause(wait, stop_event):
                        return
                    continue
                raise RuntimeError(f"HTTP {e.code}: {raw[:200]}")
        if resp is None:
            raise RuntimeError("no response")
        self._add("requests", 1)
        n_out, first = 0, None
        strip = _StripCiteMarkers()
        try:
            for raw in resp:
                if stop_event is not None and stop_event.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                kind = ev.get("type", "")
                if kind == "response.output_text.delta" and ev.get("delta"):
                    first = first or time.time()
                    piece = strip.feed(ev["delta"])
                    if piece:
                        yield Delta(kind="text", text=piece)
                elif kind == "response.output_item.done":
                    it = ev.get("item") or {}
                    if it.get("type") == "function_call":
                        try:
                            args = json.loads(it.get("arguments") or "{}")
                        except ValueError:
                            args = {}
                        yield Delta(kind="tool_call", tool_call=P.ToolCall(
                            name=it.get("name", ""),
                            arguments=args if isinstance(args, dict) else {"input": args},
                            id=it.get("call_id") or it.get("id") or f"call_{time.time_ns()}"))
                elif kind == "response.completed":
                    u = (ev.get("response") or {}).get("usage") or {}
                    n_out = int(u.get("output_tokens", 0) or 0)
                    det = u.get("input_tokens_details") or {}
                    p_in = int(u.get("input_tokens", 0) or 0)
                    p_rd = int(det.get("cached_tokens", 0) or 0)
                    p_wr = int(det.get("cache_write_tokens", 0) or 0)
                    self._add("in", p_in)
                    self._add("cache_read", p_rd)
                    self._add("cache_write", p_wr)
                    ledger.record(body.get("model", self._model()), "think" if body["reasoning"]["effort"] != "none" else "step", p_in, p_rd, p_wr, n_out)
                elif kind in ("response.failed", "error"):
                    msg = ((ev.get("response") or {}).get("error") or ev.get("error")
                           or {}).get("message", "failed")
                    raise RuntimeError(msg)
        finally:
            try:
                resp.close()
            except Exception:
                pass
        self._add("out", n_out)
        el = time.time() - first if first else 0
        yield Delta(kind="done", n_tokens=n_out, tps=n_out / el if el > 0.05 else 0.0)

    def complete(self, messages, tools=None, think=False, max_tokens=None,
                 temperature=None, stop_event=None) -> Completion:
        c = Completion()
        parts = []
        for d in self.stream(messages, tools, think, max_tokens, temperature, stop_event):
            if d.kind == "text":
                parts.append(d.text)
            elif d.kind == "tool_call":
                c.tool_calls.append(d.tool_call)
            elif d.kind == "done":
                c.n_tokens, c.tps = d.n_tokens, d.tps
        c.text = "".join(parts).strip()
        return c

    def describe_images(self, prompt: str, images: list[str], max_tokens: int = 700) -> str:
        import base64
        from pathlib import Path
        content = [{"type": "text", "text": prompt}]
        for p in images[:4]:
            mime = "image/jpeg" if p.lower().endswith((".jpg", ".jpeg")) else "image/png"
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64," + base64.b64encode(Path(p).read_bytes()).decode()}})
        body = {"model": self.model_id, "reasoning_effort": "none",
                "messages": [{"role": "user", "content": content}],
                "max_completion_tokens": max(max_tokens, 1024)}
        req = urllib.request.Request(self.endpoint + "openai/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + _TOKEN.get()})
        with http_pool.urlopen(req, timeout=120) as r:
            d = json.load(r)
        u = d.get("usage") or {}
        det = u.get("prompt_tokens_details") or {}
        ledger.record(self.model_id, "vision", int(u.get("prompt_tokens", 0) or 0),
                      int(det.get("cached_tokens", 0) or 0),
                      int(det.get("cache_write_tokens", 0) or 0),
                      int(u.get("completion_tokens", 0) or 0))
        return d["choices"][0]["message"].get("content") or ""

    # -- parity ------------------------------------------------------------
    def load(self) -> None:
        if not getattr(self, "_keepalive", False) and self.endpoint.strip("/"):
            self._keepalive = True
            try:
                http_pool.keep_warm(self.endpoint + "openai/v1/models",
                                    {"Authorization": "Bearer " + _TOKEN.get()})
            except Exception:
                pass
        if not self._checked:
            self._checked = True
            try:
                _TOKEN.get()
                log.info("azure backend ready: %s @ %s", self.model_id, self.endpoint)
            except Exception as e:
                log.error("azure sign-in problem: %s", e)

    def warm(self, use_disk=True) -> float:
        t0 = time.time()
        self.load()
        return time.time() - t0

    def touch_weights(self, cancel=None) -> float:
        return 0.0

    def unload(self) -> None:
        pass

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _model(self) -> str:
        return self.turn_model or self.model_id

    def _add(self, key: str, n) -> None:
        self._usage[key] += n
        m = self._by_model.setdefault(self._model(), {"in": 0, "out": 0, "cache_read": 0,
                                                      "cache_write": 0, "requests": 0})
        m[key] = m.get(key, 0) + n

    @property
    def usage(self) -> dict:
        d = dict(self._usage)
        d["by_model"] = {m: dict(u) for m, u in self._by_model.items()}
        return d
