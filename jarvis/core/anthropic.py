"""
Anthropic (Claude) backend.

Same `stream()` contract as the local MLX backend, so the agent, the tools and
the daemon are untouched by the switch.

Three things make this fast enough to be the default:

* **Real SSE streaming.** Tokens are yielded as they arrive, so time-to-first-
  token is one round trip (~0.4-0.9 s) rather than the whole completion.
* **Prompt caching.** The system prompt plus ~60 tool schemas is a ~5k token
  constant that would otherwise be re-read on every step of every turn. A
  `cache_control` breakpoint on the last tool makes it a cache *read* after the
  first call - about a tenth of the price and materially faster to first token.
  A second breakpoint on the tail of the conversation means each agent step
  reads back the previous step instead of reprocessing it.
* **Server-side tool calling.** No custom wire format to parse and no
  reasoning-channel leakage: tool calls arrive as structured `tool_use` blocks.

The key lives in ~/.jarvis/secrets.env (mode 600), never in the repo.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

from ..config import CONFIG
from . import protocol as P
from .cloud import load_secrets
from .llm import Completion, Delta

log = logging.getLogger("jarvis.claude")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Cheapest model that can actually drive this tool set. Haiku 4.5 does
# multi-step tool use, vision and structured output at a fraction of Sonnet's
# price; JARVIS_CLAUDE_MODEL overrides it when a task wants more reasoning.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
SMART_MODEL = "claude-sonnet-5"


def api_key() -> str:
    s = load_secrets()
    return (os.environ.get("ANTHROPIC_API_KEY")
            or s.get("ANTHROPIC_API_KEY")
            or s.get("CLAUDE_API_KEY") or "")


class AnthropicBackend:
    """Drop-in replacement for MLXGemmaBackend, backed by the Claude API."""

    def __init__(self, model: str = "", key: str = ""):
        self.key = key or api_key()
        self.model_id = (model or os.environ.get("JARVIS_CLAUDE_MODEL")
                         or DEFAULT_MODEL)
        self._lock = threading.RLock()
        self._checked = False
        self._usage = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}

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
    def _headers(self) -> dict:
        return {
            "content-type": "application/json",
            "x-api-key": self.key,
            "anthropic-version": API_VERSION,
        }

    def _post(self, body: dict, timeout: int = 120, stream: bool = False):
        """POST /v1/messages. Returns the raw response when streaming."""
        req = urllib.request.Request(
            API_URL, data=json.dumps(body).encode(), headers=self._headers())
        return urllib.request.urlopen(req, timeout=timeout)

    # -- translation -------------------------------------------------------
    @staticmethod
    def _to_tools(tools: list[dict] | None) -> list[dict]:
        """OpenAI-style tool schemas -> Anthropic `tools`."""
        if not tools:
            return []
        out = []
        for t in tools:
            fn = t.get("function", t)
            params = fn.get("parameters") or {"type": "object", "properties": {}}
            if not isinstance(params, dict) or "type" not in params:
                params = {"type": "object", "properties": {}}
            out.append({
                "name": fn.get("name", ""),
                "description": (fn.get("description") or "")[:1500],
                "input_schema": params,
            })
        # One breakpoint after the last tool caches system + every schema.
        if out:
            out[-1] = dict(out[-1], cache_control={"type": "ephemeral"})
        return out

    @staticmethod
    def _to_messages(messages: list[dict]) -> tuple[list[dict], str]:
        """
        Our message list -> Anthropic `messages`, plus the system prompt.

        Anthropic rejects a request whose `tool_use` and `tool_result` blocks do
        not pair up exactly, and the agent's context trimming can drop either
        side. Rather than 400 on a trimmed conversation, unmatched blocks are
        repaired: an orphaned result becomes plain text, and a call with no
        result gets a synthetic one saying so.
        """
        system = ""
        out: list[dict] = []
        open_ids: list[str] = []          # tool_use ids awaiting a result

        def close_open(reason: str) -> None:
            """Satisfy any tool_use left dangling by trimming or an interrupt."""
            if not open_ids:
                return
            blocks = [{"type": "tool_result", "tool_use_id": i,
                       "content": reason, "is_error": True} for i in open_ids]
            out.append({"role": "user", "content": blocks})
            open_ids.clear()

        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""

            if role == "system":
                system = content
                continue

            if role == "tool":
                tid = m.get("tool_call_id") or ""
                block = {"type": "tool_result", "tool_use_id": tid,
                         "content": str(content)[:16000] or "(no output)"}
                if tid in open_ids:
                    open_ids.remove(tid)
                    if out and out[-1]["role"] == "user" and \
                            isinstance(out[-1]["content"], list) and \
                            out[-1]["content"][0].get("type") == "tool_result":
                        out[-1]["content"].append(block)
                    else:
                        out.append({"role": "user", "content": [block]})
                else:
                    # orphaned by trimming - keep the information, drop the link
                    name = m.get("name") or "tool"
                    out.append({"role": "user", "content": [
                        {"type": "text",
                         "text": f"[earlier result from {name}] "
                                 f"{str(content)[:4000]}"}]})
                continue

            if role == "assistant":
                blocks: list[dict] = []
                text = str(content).strip()
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", tc)
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {"input": args}
                    tid = tc.get("id") or f"toolu_{len(out)}_{len(blocks)}"
                    blocks.append({"type": "tool_use", "id": tid,
                                   "name": fn.get("name", ""), "input": args})
                    open_ids.append(tid)
                if not blocks:
                    continue
                close_needed = [b for b in blocks if b["type"] == "tool_use"]
                out.append({"role": "assistant", "content": blocks})
                if not close_needed:
                    open_ids.clear()
                continue

            # user
            close_open("(no result: the previous step was interrupted)")
            text = str(content).strip()
            if text:
                out.append({"role": "user", "content": [
                    {"type": "text", "text": text}]})

        close_open("(no result recorded)")

        # Roles must alternate. Repairing a dangling tool_use, or a turn that
        # produced only tool calls, can leave two user turns adjacent - merge
        # rather than let the API reject the whole request.
        merged: list[dict] = []
        for m in out:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"].extend(m["content"])
            else:
                merged.append({"role": m["role"], "content": list(m["content"])})
        out = merged

        # The API requires a non-empty conversation starting with a user turn.
        while out and out[0]["role"] != "user":
            out.pop(0)
        if not out:
            out = [{"role": "user", "content": [{"type": "text", "text": "Hello."}]}]

        # Second cache breakpoint on the tail: each agent step then reads back
        # the previous step's prefix instead of reprocessing the whole history.
        if len(out) > 2 and isinstance(out[-1]["content"], list):
            out[-1]["content"][-1] = dict(out[-1]["content"][-1],
                                          cache_control={"type": "ephemeral"})
        return out, system

    # -- generation --------------------------------------------------------
    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               think: bool = False, max_tokens: int | None = None,
               temperature: float | None = None,
               stop_event: threading.Event | None = None) -> Iterator[Delta]:
        if not self.key:
            yield Delta(kind="text", text="(no Anthropic API key configured)")
            yield Delta(kind="done")
            return

        msgs, system = self._to_messages(messages)
        temp = (CONFIG.llm.tool_temperature if tools else CONFIG.llm.temperature) \
            if temperature is None else temperature

        body: dict = {
            "model": self.model_id,
            "max_tokens": max_tokens or CONFIG.llm.max_tokens,
            "messages": msgs,
            "temperature": max(0.0, min(1.0, temp)),
            "stream": True,
        }
        if system:
            # The cache prefix is ordered tools -> system -> messages, so the
            # breakpoint that covers *both* the schemas and the system prompt
            # belongs on the system block, not on the last tool.
            body["system"] = [{"type": "text", "text": system,
                               "cache_control": {"type": "ephemeral"}}]
        decls = self._to_tools(tools)
        if decls:
            body["tools"] = decls

        for attempt in range(4):
            try:
                yield from self._stream_once(body, stop_event)
                return
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                    wait = 1.5 * (2 ** attempt)
                    log.warning("claude %s; retrying in %.1fs", e.code, wait)
                    time.sleep(wait)
                    continue
                try:
                    msg = json.loads(raw)["error"]["message"]
                except Exception:
                    msg = raw[:300] or f"HTTP {e.code}"
                log.error("claude error %s: %s", e.code, msg)
                yield Delta(kind="text", text=f"(Claude API error: {msg[:250]})")
                yield Delta(kind="done")
                return
            except Exception as e:
                if attempt < 3:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                log.exception("claude request failed")
                yield Delta(kind="text", text=f"(Claude request failed: {e})")
                yield Delta(kind="done")
                return

    def _stream_once(self, body: dict,
                     stop_event: threading.Event | None) -> Iterator[Delta]:
        t0 = time.time()
        n_out = 0
        blocks: dict[int, dict] = {}
        resp = self._post(body, stream=True)
        try:
            for raw in resp:
                if stop_event is not None and stop_event.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                kind = ev.get("type")

                if kind == "content_block_start":
                    cb = ev.get("content_block") or {}
                    blocks[ev.get("index", 0)] = {
                        "type": cb.get("type"), "id": cb.get("id", ""),
                        "name": cb.get("name", ""), "json": ""}

                elif kind == "content_block_delta":
                    d = ev.get("delta") or {}
                    dt = d.get("type")
                    if dt == "text_delta":
                        yield Delta(kind="text", text=d.get("text", ""))
                    elif dt == "thinking_delta":
                        yield Delta(kind="thought", text=d.get("thinking", ""))
                    elif dt == "input_json_delta":
                        b = blocks.setdefault(ev.get("index", 0),
                                              {"type": "tool_use", "json": ""})
                        b["json"] = b.get("json", "") + d.get("partial_json", "")

                elif kind == "content_block_stop":
                    b = blocks.pop(ev.get("index", 0), None)
                    if b and b.get("type") == "tool_use":
                        try:
                            args = json.loads(b.get("json") or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        if not isinstance(args, dict):
                            args = {"input": args}
                        yield Delta(kind="tool_call", tool_call=P.ToolCall(
                            name=b.get("name", ""), arguments=args,
                            id=b.get("id") or f"toolu_{time.time_ns()}"))

                elif kind == "message_delta":
                    u = ev.get("usage") or {}
                    n_out = int(u.get("output_tokens", n_out) or n_out)

                elif kind == "message_start":
                    u = (ev.get("message") or {}).get("usage") or {}
                    self._usage["in"] += int(u.get("input_tokens", 0) or 0)
                    self._usage["cache_read"] += int(
                        u.get("cache_read_input_tokens", 0) or 0)
                    self._usage["cache_write"] += int(
                        u.get("cache_creation_input_tokens", 0) or 0)

                elif kind == "error":
                    msg = (ev.get("error") or {}).get("message", "stream error")
                    yield Delta(kind="text", text=f"(Claude: {msg[:200]})")
        finally:
            try:
                resp.close()
            except Exception:
                pass

        self._usage["out"] += n_out
        el = max(time.time() - t0, 1e-3)
        yield Delta(kind="done", n_tokens=n_out, tps=n_out / el)

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
        import base64
        blocks: list[dict] = []
        for path in images[:4]:
            p = Path(path)
            data = p.read_bytes()
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "gif": "image/gif",
                    "webp": "image/webp"}.get(p.suffix.lower().lstrip("."),
                                              "image/png")
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": mime,
                "data": base64.b64encode(data).decode()}})
        blocks.append({"type": "text", "text": prompt})
        body = {"model": self.model_id, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": blocks}]}
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(),
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            raise RuntimeError(f"Claude vision error {e.code}: {raw[:200]}")
        return "".join(b.get("text", "") for b in d.get("content", [])
                       if b.get("type") == "text")

    # -- parity with the local backend -------------------------------------
    def load(self) -> None:
        """Nothing to load. Verify the key once so failures surface at start."""
        with self._lock:
            if self._checked or not self.key:
                return
            self._checked = True
            try:
                body = {"model": self.model_id, "max_tokens": 4,
                        "messages": [{"role": "user", "content": "hi"}]}
                req = urllib.request.Request(
                    API_URL, data=json.dumps(body).encode(),
                    headers=self._headers())
                with urllib.request.urlopen(req, timeout=30):
                    log.info("claude backend ready: %s", self.model_id)
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")[:200]
                log.error("claude key check failed (%s): %s", e.code, raw)
            except Exception as e:
                log.warning("claude key check skipped: %s", e)

    def warm(self, use_disk: bool = True) -> float:
        """
        Prime the prompt cache with the real system+tools prefix.

        The first call after a cold cache pays full input price and latency for
        ~5k tokens; paying it here means the user's first question does not.
        """
        t0 = time.time()
        if not self.key:
            return 0.0
        try:
            from ..tools.registry import REGISTRY
            from . import prompts
            for _ in self.stream(
                    [{"role": "system", "content": prompts.system_prompt()},
                     {"role": "user", "content": "Reply with the word ready."}],
                    tools=REGISTRY.schemas(), max_tokens=8, temperature=0.0):
                pass
        except Exception as e:
            log.warning("claude warm failed: %s", e)
        return time.time() - t0

    def touch_weights(self, cancel: threading.Event | None = None) -> float:
        """No local weights to keep resident."""
        return 0.0

    def unload(self) -> None:
        pass

    def count_tokens(self, text: str) -> int:
        """
        Cheap local estimate. The counting endpoint is a network round trip and
        this is only used to decide when to trim history, where being a few
        percent out costs nothing.
        """
        return max(1, len(text) // 4)

    @property
    def usage(self) -> dict:
        return dict(self._usage)
