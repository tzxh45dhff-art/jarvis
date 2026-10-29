"""
Gemini backend (Google Generative Language API).

Same `stream()` contract as the local MLX backend, so the agent, tools, daemon
and UI do not know or care which one is running.

Measured on this machine before choosing the defaults below (October 2026):

  model                    tool call   notes
  gemini-3.8-flash         2.2 s       newest Flash - the default (Oct 11)
  gemini-3.5-flash         2.4 s       fallback
  gemini-3.7-flash         2.7 s       fallback
  gemini-3.5-flash-lite    1.3 s       too weak for multi-step computer use
  gemini-3.1-pro-preview   -           zero quota on the free tier

Things this file exists to get right, each learned from a real failure:

* **Thought signatures.** Gemini 3.x attaches a `thoughtSignature` to the first
  function call of every step and rejects the *next* step with HTTP 400 if it
  is not sent back verbatim. The previous version of this file dropped them, so
  every task needing two tool steps failed. They now ride on the ToolCall and
  through the agent's message history untouched.
* **History from another model.** After `/backend cloud` the conversation holds
  tool calls Gemma made, which have no signature. Google's documented bypass
  value is filled in for those, so switching never breaks a conversation.
* **Schemas.** Tool parameters go through `parametersJsonSchema`, which accepts
  the registry's JSON Schema as-is - no lossy conversion to Google's OpenAPI
  subset.
* **Temperature.** Not sent unless a caller asks for one: Google advises that
  lowering it on Gemini 3 causes looping, and the agent's tool temperature of 0
  was tuned for the local model.
* **Streaming.** Server-sent events, so text reaches the screen as it is
  generated rather than after the whole reply.

The key is read from ~/.jarvis/secrets.env (mode 600) or the environment, and is
sent as a header - never in a URL, where it would end up in logs.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

from ..config import CONFIG, JARVIS_HOME
from . import protocol as P
from .llm import Completion, Delta

log = logging.getLogger("jarvis.cloud")

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
SECRETS = JARVIS_HOME / "secrets.env"

# Chosen by the user (2026-10-11) after Flash-Lite proved too weak for
# multi-step computer use. All three are full Flash models, each with its own
# free-tier daily quota, so one running dry never drops JARVIS to a Lite model.
DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACKS = ("gemini-3.5-flash", "gemini-3.7-flash")
# Only when every Flash model is rate-limited. Measured on this key's free tier:
# Flash models allow 5 requests/minute and (3.8) 20/day, so without these JARVIS
# simply stops answering after a few tasks. Weaker, but it keeps working.
LAST_RESORT = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")

# Google's documented value for function calls the model did not itself produce
# (history from another model, or a request replayed on a fallback model).
BYPASS_SIGNATURE = "skip_thought_signature_validator"

# Long tool arguments - a whole report passed to write_document - count as
# output tokens, and the agent's local default of 4096 truncates them.
MIN_OUTPUT_TOKENS = 16384

_RETRYABLE = {429, 500, 502, 503, 504}


def load_secrets() -> dict[str, str]:
    """API keys, from (later wins) ~/.jarvis/secrets.env, ~/.jarvis/.env, the
    repo's .env, then the environment. Never exported back into os.environ."""
    from ..config import REPO_ROOT, read_env_file
    out: dict[str, str] = {}
    for path in (SECRETS, JARVIS_HOME / ".env", REPO_ROOT / ".env"):
        out.update(read_env_file(path))
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def api_key() -> str:
    s = load_secrets()
    return s.get("GEMINI_API_KEY") or s.get("GOOGLE_API_KEY") or ""


class _HTTPError(Exception):
    def __init__(self, code: int, message: str, details: list | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or []

    def quota(self) -> list[dict]:
        """Which quota was exceeded and its limit, straight from Google."""
        out = []
        for d in self.details:
            for v in d.get("violations", []) or []:
                if "quotaId" in v or "quotaMetric" in v:
                    out.append({"id": v.get("quotaId", ""),
                                "metric": v.get("quotaMetric", "").split("/")[-1],
                                "limit": v.get("quotaValue")})
        return out


# A model that has run out of quota is skipped until it resets, instead of
# being retried on every step - which cost ~11 s per step for a whole evening
# while Flash-Lite's daily allowance was gone. Shared by every backend object.
_COOLDOWN: dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()


def _next_pacific_midnight() -> float:
    """Daily quotas reset at midnight Pacific time."""
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0,
                                                microsecond=0)
        return nxt.timestamp()
    except Exception:
        return time.time() + 3600


def cooling(model: str) -> float:
    """Seconds until `model` may be tried again (0 = usable now)."""
    with _COOLDOWN_LOCK:
        return max(0.0, _COOLDOWN.get(model, 0.0) - time.time())


def _cool(model: str, err: "_HTTPError") -> str:
    q = err.quota()
    ids = " ".join(x["id"] for x in q)
    if "PerDay" in ids or "per_day" in ids.lower():
        until, why = _next_pacific_midnight(), "daily quota used up"
    elif "PerMinute" in ids or not q:
        until, why = time.time() + 60, "per-minute limit hit"
    else:
        until, why = time.time() + 300, "quota limit hit"
    with _COOLDOWN_LOCK:
        _COOLDOWN[model] = until
    limits = ", ".join(f"{x['id'] or x['metric']}={x['limit']}" for x in q if x.get("limit"))
    log.warning("%s: %s%s - skipping it for %.0f min", model, why,
                f" (limit {limits})" if limits else "", (until - time.time()) / 60)
    return why


class GeminiBackend:
    """Drop-in replacement for MLXGemmaBackend, backed by the Gemini API."""

    def __init__(self, model: str = "", key: str = ""):
        self.key = key or api_key()
        self.model_id = (model or os.environ.get("JARVIS_CLOUD_MODEL")
                         or DEFAULT_MODEL)
        self._lock = threading.RLock()
        self._checked = False
        self._last_tools: list[dict] | None = None
        self._last_usage: dict = {}
        self._usage = {"in": 0, "out": 0, "cache_read": 0, "requests": 0}

    # Older code reads `_model`; keep it pointing at the same thing.
    @property
    def _model(self) -> str:
        return self.model_id

    # -- availability ------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.key)

    @property
    def ready(self) -> bool:
        return bool(self.key)

    def wait_ready(self, timeout: float | None = None) -> bool:
        return bool(self.key)

    # -- http --------------------------------------------------------------
    def _request(self, path: str, body: dict | None = None,
                 timeout: float = 120):
        req = urllib.request.Request(
            f"{API_ROOT}/{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": self.key},
            method="POST" if body is not None else "GET")
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            details = []
            try:
                err = json.loads(raw)["error"]
                msg, details = err["message"], err.get("details") or []
            except Exception:
                msg = raw[:300] or f"HTTP {e.code}"
            raise _HTTPError(e.code, msg, details) from None

    @staticmethod
    def _explain(code: int, msg: str) -> str:
        """Turn an API failure into something a person can act on."""
        if code == 402 or "credits are depleted" in msg:
            return ("the Gemini project's prepaid credit is used up - top it up "
                    "at ai.studio/projects, or switch with /backend local")
        if code in (401, 403):
            return f"Gemini rejected the API key ({msg[:120]})"
        if code == 429:
            return "Gemini rate limit reached - wait a moment and try again"
        return msg[:240]

    # -- translation -------------------------------------------------------
    @staticmethod
    def _to_declarations(tools: list[dict] | None) -> list[dict]:
        if not tools:
            return []
        decls = []
        for t in tools:
            fn = t.get("function", t)
            params = fn.get("parameters") or {"type": "object", "properties": {}}
            decls.append({"name": fn.get("name", ""),
                          "description": (fn.get("description") or "")[:2000],
                          "parametersJsonSchema": params})
        return [{"functionDeclarations": decls}]

    @staticmethod
    def _tool_payload(content) -> dict:
        """A tool result as the `response` object Gemini expects."""
        try:
            obj = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError):
            obj = content
        return {"output": obj if obj not in (None, "") else "(no output)"}

    @classmethod
    def _to_contents(cls, messages: list[dict],
                     bypass_all: bool = False) -> tuple[list[dict], str]:
        """
        Agent messages -> Gemini `contents`, plus the system instruction.

        Gemini rejects a function response that does not directly follow its
        call, and history trimming can remove either half. Orphaned results
        become plain text; calls left without a result get a synthetic one.
        """
        system = ""
        out: list[dict] = []
        open_calls: dict[str, str] = {}        # call id -> tool name

        def close_open(reason: str) -> None:
            if not open_calls:
                return
            out.append({"role": "user", "parts": [
                {"functionResponse": {"name": name, "id": cid,
                                      "response": {"error": reason}}}
                for cid, name in open_calls.items()]})
            open_calls.clear()

        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""

            if role == "system":
                system = content
                continue

            if role == "tool":
                cid = m.get("tool_call_id") or ""
                name = m.get("name") or open_calls.get(cid) or "tool"
                if cid in open_calls:
                    open_calls.pop(cid)
                    out.append({"role": "user", "parts": [{"functionResponse": {
                        "name": name, "id": cid,
                        "response": cls._tool_payload(content)}}]})
                else:
                    out.append({"role": "user", "parts": [{
                        "text": f"[earlier result from {name}] "
                                f"{str(content)[:4000]}"}]})
                continue

            if role == "assistant":
                parts: list[dict] = []
                text = str(content).strip()
                if text:
                    parts.append({"text": text})
                first_call = True
                for i, tc in enumerate(m.get("tool_calls") or []):
                    fn = tc.get("function", tc)
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"input": args}
                    cid = tc.get("id") or f"call_{len(out)}_{i}"
                    part = {"functionCall": {"name": fn.get("name", ""),
                                             "args": args, "id": cid}}
                    sig = tc.get("thought_signature")
                    if bypass_all and first_call:
                        sig = BYPASS_SIGNATURE
                    elif not sig and first_call:
                        sig = BYPASS_SIGNATURE     # call made by another model
                    if sig:
                        part["thoughtSignature"] = sig
                    first_call = False
                    parts.append(part)
                    open_calls[cid] = fn.get("name", "")
                if parts:
                    out.append({"role": "model", "parts": parts})
                continue

            # user
            close_open("no result: the step was interrupted")
            text = str(content).strip()
            if text:
                out.append({"role": "user", "parts": [{"text": text}]})

        close_open("no result recorded")

        merged: list[dict] = []
        for c in out:
            if merged and merged[-1]["role"] == c["role"]:
                merged[-1]["parts"].extend(c["parts"])
            else:
                merged.append({"role": c["role"], "parts": list(c["parts"])})
        while merged and merged[0]["role"] != "user":
            merged.pop(0)
        if not merged:
            merged = [{"role": "user", "parts": [{"text": "Hello."}]}]
        return merged, system

    # -- generation --------------------------------------------------------
    def _body(self, messages, tools, think, max_tokens, temperature,
              bypass_all=False) -> dict:
        contents, system = self._to_contents(messages, bypass_all=bypass_all)
        # A caller asking for a short reply (a wrap-up, a check) gets exactly
        # that; the agent's general default is raised so long tool arguments
        # are not cut off.
        limit = max_tokens or CONFIG.llm.max_tokens
        if limit >= CONFIG.llm.max_tokens:
            limit = max(limit, MIN_OUTPUT_TOKENS)
        gen: dict = {"maxOutputTokens": limit}
        if temperature is not None:
            gen["temperature"] = temperature
        if think:
            gen["thinkingConfig"] = {"thinkingLevel": "high"}
        body: dict = {"contents": contents, "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        if tools:
            self._last_tools = tools
            body["tools"] = self._to_declarations(tools)
        elif self._last_tools and any(
                "functionCall" in p for c in contents for p in c["parts"]):
            # A wrap-up turn ("stop calling tools and explain") still carries
            # tool history; declare the tools but forbid calling them.
            body["tools"] = self._to_declarations(self._last_tools)
            body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        return body

    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               think: bool = False, max_tokens: int | None = None,
               temperature: float | None = None,
               stop_event: threading.Event | None = None) -> Iterator[Delta]:
        if not self.key:
            yield Delta(kind="text", text="(no Gemini API key configured)")
            yield Delta(kind="done")
            return

        models = [self.model_id] + [m for m in FALLBACKS if m != self.model_id]
        bypass_all = False
        empty_retried = False
        last_err = ""
        usable = [m for m in models if not cooling(m)]
        if not usable:
            usable = [m for m in LAST_RESORT if not cooling(m)]
            if usable:
                log.warning("all Flash models rate-limited; using %s as a last "
                            "resort", usable[0])
        if not usable:
            models = models + [m for m in LAST_RESORT if m not in models]
            soonest = min(models, key=cooling)
            wait = cooling(soonest)
            yield Delta(kind="text", text=(
                f"(Every Gemini model is out of quota - the next frees up in "
                f"{wait / 60:.0f} min. Switch with /backend local meanwhile.)"))
            yield Delta(kind="done")
            return
        for model in usable:
            for attempt in range(3):
                body = self._body(messages, tools, think, max_tokens,
                                  temperature, bypass_all=bypass_all)
                produced = [False]
                try:
                    done = None
                    for d in self._stream_once(model, body, stop_event,
                                               produced):
                        if d.kind == "done":
                            done = d          # held back until we know it
                        else:                 # was not an empty reply
                            yield d
                    stopped = stop_event is not None and stop_event.is_set()
                    if not produced[0] and not stopped and not empty_retried:
                        # Gemini occasionally answers a tool result with no
                        # text and no call at all, which reaches the user as a
                        # blank reply. One retry has recovered it every time.
                        empty_retried = True
                        log.warning("empty reply from %s; retrying once", model)
                        continue
                    if model != self.model_id:
                        log.warning("answered by fallback model %s", model)
                    yield done or Delta(kind="done")
                    return
                except _HTTPError as e:
                    last_err = self._explain(e.code, e.message)
                    if e.code == 400 and "signature" in e.message.lower() \
                            and not bypass_all:
                        # Signatures from one model do not validate on another.
                        log.warning("signature rejected on %s; retrying with "
                                    "the bypass value", model)
                        bypass_all = True
                        continue
                    if e.code == 429 and ("quota" in e.message.lower() or e.quota()):
                        _cool(model, e)
                        self._usage.setdefault("quota_hits", {})[model] = \
                            self._usage.get("quota_hits", {}).get(model, 0) + 1
                        break                      # next model, no waiting
                    if e.code in _RETRYABLE:
                        if attempt == 2:
                            break                  # give the next model a go
                        wait = 0.8 * (2 ** attempt)
                        log.warning("gemini %s on %s: %s - retry in %.1fs",
                                    e.code, model, e.message[:80], wait)
                        time.sleep(wait)
                        continue
                    if e.code == 404:
                        log.warning("model %s unavailable: %s", model, e.message[:100])
                        break                      # next model
                    log.error("gemini error %s: %s", e.code, e.message)
                    yield Delta(kind="text", text=f"(Gemini: {last_err})")
                    yield Delta(kind="done")
                    return
                except Exception as e:
                    last_err = str(e)
                    if produced[0]:
                        # Text already reached the screen; a retry would
                        # repeat it. Say what happened instead.
                        log.warning("stream dropped mid-reply: %s", e)
                        yield Delta(kind="text",
                                    text="\n(connection to Gemini dropped)")
                        yield Delta(kind="done")
                        return
                    time.sleep(0.8 * (attempt + 1))
                    continue
            # retries exhausted, or the model is gone: try the next one
        yield Delta(kind="text", text=f"(Gemini unavailable: {last_err})")
        yield Delta(kind="done")

    def _stream_once(self, model: str, body: dict,
                     stop_event: threading.Event | None,
                     produced: list) -> Iterator[Delta]:
        first_out = None
        n_out = 0
        n_calls = 0
        resp = self._request(f"models/{model}:streamGenerateContent?alt=sse",
                             body)
        self._usage["requests"] += 1
        by = self._usage.setdefault("by_model", {})
        by[model] = by.get(model, 0) + 1
        try:
            for raw in resp:
                if stop_event is not None and stop_event.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    chunk = json.loads(line[5:].strip())
                except ValueError:
                    continue

                block = (chunk.get("promptFeedback") or {}).get("blockReason")
                if block:
                    produced[0] = True
                    yield Delta(kind="text",
                                text=f"(Gemini declined the request: {block})")

                for cand in chunk.get("candidates") or []:
                    for part in (cand.get("content") or {}).get("parts") or []:
                        if first_out is None and (part.get("text") or
                                                  "functionCall" in part):
                            first_out = time.time()
                        if "functionCall" in part:
                            fc = part["functionCall"]
                            args = fc.get("args") or {}
                            if not isinstance(args, dict):
                                args = {"input": args}
                            produced[0] = True
                            yield Delta(kind="tool_call", tool_call=P.ToolCall(
                                name=fc.get("name", ""), arguments=args,
                                id=fc.get("id") or f"call_{time.time_ns()}_{n_calls}",
                                signature=part.get("thoughtSignature", "")))
                            n_calls += 1
                        elif part.get("text"):
                            produced[0] = True
                            yield Delta(kind="thought" if part.get("thought")
                                        else "text", text=part["text"])
                    reason = cand.get("finishReason")
                    if reason and reason not in ("STOP", "MAX_TOKENS"):
                        log.warning("gemini finished with %s", reason)

                u = chunk.get("usageMetadata")
                if u:
                    n_out = int(u.get("candidatesTokenCount", 0) or 0) + \
                        int(u.get("thoughtsTokenCount", 0) or 0)
                    self._last_usage = u
        finally:
            try:
                resp.close()
            except Exception:
                pass

        u = self._last_usage or {}
        self._usage["in"] += int(u.get("promptTokenCount", 0) or 0)
        self._usage["cache_read"] += int(u.get("cachedContentTokenCount", 0) or 0)
        self._usage["out"] += n_out
        self._last_usage = {}
        # Generation rate, not network wait: measured from the first output.
        el = time.time() - first_out if first_out else 0.0
        yield Delta(kind="done", n_tokens=n_out,
                    tps=n_out / el if el > 0.05 else 0.0)

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 think: bool = False, max_tokens: int | None = None,
                 temperature: float | None = None,
                 stop_event: threading.Event | None = None) -> Completion:
        c = Completion()
        parts, thoughts = [], []
        t0 = time.time()
        for d in self.stream(messages, tools, think, max_tokens, temperature,
                             stop_event):
            if d.kind == "text":
                if not parts:
                    c.ttft = time.time() - t0
                parts.append(d.text)
            elif d.kind == "thought":
                thoughts.append(d.text)
            elif d.kind == "tool_call":
                c.tool_calls.append(d.tool_call)
            elif d.kind == "done":
                c.tps, c.n_tokens = d.tps, d.n_tokens
        c.text = "".join(parts).strip()
        c.thought = "\n".join(thoughts).strip()
        return c

    # -- vision ------------------------------------------------------------
    def describe_images(self, prompt: str, images: list[str],
                        max_tokens: int = 700) -> str:
        parts: list[dict] = [{"text": prompt}]
        for path in images[:4]:
            p = Path(path)
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "gif": "image/gif",
                    "heic": "image/heic"}.get(p.suffix.lower().lstrip("."),
                                              "image/png")
            parts.append({"inlineData": {
                "mimeType": mime,
                "data": base64.b64encode(p.read_bytes()).decode()}})
        body = {"contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"maxOutputTokens": max(max_tokens, 1024)}}
        last = ""
        for model in [self.model_id] + list(FALLBACKS):
            for attempt in range(3):
                try:
                    with self._request(f"models/{model}:generateContent",
                                       body) as r:
                        d = json.load(r)
                    out = (d.get("candidates") or [{}])[0] \
                        .get("content", {}).get("parts", [])
                    return "".join(x.get("text", "") for x in out
                                   if not x.get("thought"))
                except _HTTPError as e:
                    last = self._explain(e.code, e.message)
                    if e.code in _RETRYABLE:
                        time.sleep(0.8 * (2 ** attempt))
                        continue
                    if e.code == 404:
                        break
                    raise RuntimeError(f"Gemini vision: {last}") from None
        raise RuntimeError(f"Gemini vision unavailable: {last}")

    # -- parity with the local backend -------------------------------------
    def load(self) -> None:
        """
        Confirm the key and model once. Reading model metadata is free, so this
        catches a revoked key or a withdrawn model at startup without spending
        anything - and moves to a fallback model if the default has gone.
        """
        with self._lock:
            if self._checked or not self.key:
                return
            self._checked = True
            for model in [self.model_id] + list(FALLBACKS):
                try:
                    with self._request(f"models/{model}", timeout=15):
                        pass
                    if model != self.model_id:
                        log.warning("%s unavailable; using %s",
                                    self.model_id, model)
                        self.model_id = model
                    log.info("gemini backend ready: %s", self.model_id)
                    return
                except _HTTPError as e:
                    if e.code == 404:
                        continue
                    log.error("gemini key check failed (%s): %s", e.code,
                              e.message[:160])
                    return
                except Exception as e:
                    log.warning("gemini key check skipped: %s", e)
                    return

    def warm(self, use_disk: bool = True) -> float:
        """
        Nothing to warm. Gemini caches a repeated prompt prefix automatically,
        so the first real request primes it; paying for a throwaway request on
        every daemon start and every /new would buy nothing.
        """
        t0 = time.time()
        self.load()
        return time.time() - t0

    def touch_weights(self, cancel: threading.Event | None = None) -> float:
        return 0.0

    def unload(self) -> None:
        pass

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    @property
    def usage(self) -> dict:
        return dict(self._usage)
