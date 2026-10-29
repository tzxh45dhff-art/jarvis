"""
Codec for Gemma-4's native tool-call / reasoning wire format.

The model does not emit JSON. It emits a compact custom grammar:

    <|channel>thought\n ...reasoning... \n<channel|>
    <|tool_call>call:NAME{key:<|"|>string<|"|>,n:12,flag:true,xs:[<|"|>a<|"|>]}<tool_call|>

Strings are delimited by the literal token ``<|"|>`` with no escape mechanism,
so values are sanitised on the way out (see :func:`sanitize_string`).

This module is deliberately dependency-free and pure so it can be unit tested
without loading a 14 GB model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

# --- wire tokens -------------------------------------------------------------
STR = '<|"|>'
TOOL_CALL_OPEN = "<|tool_call>"
TOOL_CALL_CLOSE = "<tool_call|>"
TOOL_RESP_OPEN = "<|tool_response>"
TOOL_RESP_CLOSE = "<tool_response|>"
CHANNEL_OPEN = "<|channel>"
CHANNEL_CLOSE = "<channel|>"
TURN_CLOSE = "<turn|>"
TURN_OPEN = "<|turn>"

# Token ids resolved from the tokenizer (verified against the shipped vocab).
EOS_TOKEN_IDS = {1, 106, 50}

ALL_MARKERS = (
    TOOL_CALL_OPEN, TOOL_CALL_CLOSE, TOOL_RESP_OPEN, TOOL_RESP_CLOSE,
    CHANNEL_OPEN, CHANNEL_CLOSE, TURN_CLOSE, TURN_OPEN, STR,
)
_MAX_MARKER = max(len(m) for m in ALL_MARKERS)


def sanitize_string(s: str) -> str:
    """Neutralise wire tokens inside user/tool supplied text."""
    for tok in (STR, TOOL_CALL_OPEN, TOOL_CALL_CLOSE, TOOL_RESP_OPEN,
                TOOL_RESP_CLOSE, CHANNEL_OPEN, CHANNEL_CLOSE, TURN_OPEN, TURN_CLOSE):
        s = s.replace(tok, tok.replace("|", "❘"))  # light vertical bar look-alike
    return s


# --- value parsing -----------------------------------------------------------
class _Cursor:
    __slots__ = ("s", "i")

    def __init__(self, s: str, i: int = 0):
        self.s, self.i = s, i

    def peek(self, n: int = 1) -> str:
        return self.s[self.i:self.i + n]

    def starts(self, tok: str) -> bool:
        return self.s.startswith(tok, self.i)

    def skip_ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    @property
    def done(self) -> bool:
        return self.i >= len(self.s)


def _parse_value(c: _Cursor) -> Any:
    c.skip_ws()
    if c.done:
        return None
    if c.starts(STR):
        c.i += len(STR)
        end = c.s.find(STR, c.i)
        if end == -1:                      # unterminated -> take the rest
            val = c.s[c.i:]
            c.i = len(c.s)
        else:
            val = c.s[c.i:end]
            c.i = end + len(STR)
        return val
    ch = c.peek()
    if ch == "[":
        c.i += 1
        out: list[Any] = []
        while True:
            c.skip_ws()
            if c.done or c.peek() == "]":
                c.i += 1
                return out
            out.append(_parse_value(c))
            c.skip_ws()
            if c.peek() == ",":
                c.i += 1
            elif c.peek() == "]":
                c.i += 1
                return out
            elif c.done:
                return out
    if ch == "{":
        c.i += 1
        obj: dict[str, Any] = {}
        while True:
            c.skip_ws()
            if c.done or c.peek() == "}":
                c.i += 1
                return obj
            key = _parse_key(c)
            c.skip_ws()
            if c.peek() == ":":
                c.i += 1
            obj[key] = _parse_value(c)
            c.skip_ws()
            if c.peek() == ",":
                c.i += 1
            elif c.peek() == "}":
                c.i += 1
                return obj
            elif c.done:
                return obj
    # bare literal: read until a structural delimiter
    start = c.i
    depth = 0
    while c.i < len(c.s):
        ch = c.s[c.i]
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        c.i += 1
    raw = c.s[start:c.i].strip()
    return _coerce(raw)


def _parse_key(c: _Cursor) -> str:
    c.skip_ws()
    if c.starts(STR):
        v = _parse_value(c)
        return str(v)
    start = c.i
    while c.i < len(c.s) and c.s[c.i] not in ":,}":
        c.i += 1
    return c.s[start:c.i].strip()


def _coerce(raw: str) -> Any:
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_arguments(body: str) -> dict[str, Any]:
    """Parse the ``{...}`` argument body of a tool call."""
    body = body.strip()
    if not body.startswith("{"):
        body = "{" + body
    if not body.endswith("}"):
        body = body + "}"
    c = _Cursor(body)
    val = _parse_value(c)
    return val if isinstance(val, dict) else {}


# --- serialisation -----------------------------------------------------------
def format_argument(v: Any, escape_keys: bool = False) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, str):
        return f"{STR}{sanitize_string(v)}{STR}"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        inner = ",".join(
            (f"{STR}{k}{STR}" if escape_keys else str(k)) + ":" + format_argument(x, escape_keys)
            for k, x in sorted(v.items())
        )
        return "{" + inner + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(format_argument(x, escape_keys) for x in v) + "]"
    return f"{STR}{sanitize_string(str(v))}{STR}"


def format_tool_response(name: str, response: Any) -> str:
    """Render a tool result in the model's expected response block."""
    if isinstance(response, dict):
        inner = ",".join(f"{k}:{format_argument(v)}" for k, v in sorted(response.items()))
        body = f"response:{name}{{{inner}}}"
    else:
        body = f"response:{name}{{value:{format_argument(response)}}}"
    return f"{TOOL_RESP_OPEN}{body}{TOOL_RESP_CLOSE}"


# --- parsed structures -------------------------------------------------------
@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    raw: str = ""
    # Gemini 3 rejects the next step unless this comes back verbatim. Opaque to
    # every other backend, which ignore the key.
    signature: str = ""

    def to_openai(self) -> dict:
        out = {
            "id": self.id or f"call_{abs(hash(self.raw)) % 10**8}",
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }
        if self.signature:
            out["thought_signature"] = self.signature
        return out


@dataclass
class ParsedOutput:
    text: str = ""
    thought: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


_CALL_RE = re.compile(
    re.escape(TOOL_CALL_OPEN) + r"\s*call:\s*([A-Za-z0-9_.\-]+)\s*(\{.*?\})?\s*"
    + re.escape(TOOL_CALL_CLOSE),
    re.DOTALL,
)
# Tolerate a missing closing marker at end-of-generation.
_CALL_RE_LOOSE = re.compile(
    re.escape(TOOL_CALL_OPEN) + r"\s*call:\s*([A-Za-z0-9_.\-]+)\s*(\{.*)$",
    re.DOTALL,
)
_CHANNEL_RE = re.compile(
    re.escape(CHANNEL_OPEN) + r"(.*?)" + re.escape(CHANNEL_CLOSE), re.DOTALL
)


def parse_output(raw: str) -> ParsedOutput:
    """Parse a complete model generation into thought / text / tool calls."""
    out = ParsedOutput()
    thoughts: list[str] = []

    def _grab(m: re.Match) -> str:
        body = m.group(1)
        if body.startswith("thought"):
            body = body[len("thought"):]
        thoughts.append(body.strip())
        return ""

    remaining = _CHANNEL_RE.sub(_grab, raw)
    # An unterminated thought channel means the generation was cut short.
    if CHANNEL_OPEN in remaining:
        head, _, tail = remaining.partition(CHANNEL_OPEN)
        thoughts.append(tail.replace("thought", "", 1).strip())
        remaining = head
    out.thought = "\n".join(t for t in thoughts if t).strip()

    idx = 0
    for m in _CALL_RE.finditer(remaining):
        out.tool_calls.append(
            ToolCall(name=m.group(1), arguments=parse_arguments(m.group(2) or "{}"),
                     id=f"call_{len(out.tool_calls)}", raw=m.group(0))
        )
        idx = m.end()
    text_part = _CALL_RE.sub("", remaining)
    if not out.tool_calls:
        lm = _CALL_RE_LOOSE.search(remaining)
        if lm:
            out.tool_calls.append(
                ToolCall(name=lm.group(1), arguments=parse_arguments(lm.group(2)),
                         id="call_0", raw=lm.group(0))
            )
            text_part = remaining[: lm.start()]

    for tok in (TURN_CLOSE, TURN_OPEN, TOOL_RESP_OPEN, TOOL_RESP_CLOSE,
                TOOL_CALL_OPEN, TOOL_CALL_CLOSE, CHANNEL_CLOSE, CHANNEL_OPEN):
        text_part = text_part.replace(tok, "")
    out.text = text_part.strip()
    return out


# --- streaming ---------------------------------------------------------------
class StreamParser:
    """
    Incremental parser: feed raw model deltas, receive ``(kind, payload)`` events
    where kind is one of ``text``, ``thought``, ``tool_call``.

    Text is emitted as soon as it is provably outside a marker, so the UI can
    stream while tool calls are still buffered.
    """

    TEXT, THOUGHT, TOOL = "text", "thought", "tool"

    def __init__(self) -> None:
        self.buf = ""
        self.state = self.TEXT
        self.pending = ""          # accumulating thought / tool body
        self.calls: list[ToolCall] = []
        self.text_out: list[str] = []
        self.thought_out: list[str] = []
        # The model sometimes repeats the bare word "thought" just after
        # closing a reasoning channel. It is never real content there, but it
        # is indistinguishable from prose once the marker is gone, so the state
        # has to be tracked while parsing.
        self._just_closed_channel = False
        self._post_channel = ""

    def feed(self, delta: str) -> Iterator[tuple[str, Any]]:
        self.buf += delta
        yield from self._drain(final=False)

    def close(self) -> Iterator[tuple[str, Any]]:
        yield from self._drain(final=True)

    _STRAY_THOUGHT = re.compile(r"^[ \t]*thought\b[ \t]*\r?\n?")
    # longest prefix that could still turn out to be the stray token
    _STRAY_HOLD = len("  thought \n")

    def _emit_text(self, s: str, final: bool = False) -> Iterator[tuple[str, Any]]:
        if self._just_closed_channel:
            # Streaming delivers this a character at a time, so the decision has
            # to wait until enough has arrived to tell "thought\n" (a stray
            # marker) from "thoughts on..." (real prose).
            self._post_channel += s
            if not final and len(self._post_channel) < self._STRAY_HOLD:
                return
            s = self._STRAY_THOUGHT.sub("", self._post_channel, count=1)
            self._post_channel = ""
            self._just_closed_channel = False
        if s:
            self.text_out.append(s)
            yield ("text", s)

    def _drain(self, final: bool) -> Iterator[tuple[str, Any]]:
        while True:
            if self.state == self.TEXT:
                # find earliest opening marker
                i_ch = self.buf.find(CHANNEL_OPEN)
                i_tc = self.buf.find(TOOL_CALL_OPEN)
                cands = [(i, k) for i, k in ((i_ch, "ch"), (i_tc, "tc")) if i != -1]
                if cands:
                    i, kind = min(cands)
                    yield from self._emit_text(self._clean(self.buf[:i]))
                    self.buf = self.buf[i + (len(CHANNEL_OPEN) if kind == "ch"
                                             else len(TOOL_CALL_OPEN)):]
                    self.state = self.THOUGHT if kind == "ch" else self.TOOL
                    continue
                # hold back a possible partial marker
                hold = 0 if final else self._holdback(self.buf)
                if hold:
                    yield from self._emit_text(self._clean(self.buf[:-hold]))
                    self.buf = self.buf[-hold:]
                else:
                    yield from self._emit_text(self._clean(self.buf), final=final)
                    self.buf = ""
                if final and self._post_channel:
                    yield from self._emit_text("", final=True)
                return

            if self.state == self.THOUGHT:
                end = self.buf.find(CHANNEL_CLOSE)
                if end == -1:
                    if final:
                        self._push_thought(self.buf)
                        self.buf = ""
                        self.state = self.TEXT
                    return
                self._push_thought(self.buf[:end])
                self.buf = self.buf[end + len(CHANNEL_CLOSE):]
                self.state = self.TEXT
                self._just_closed_channel = True
                if self.thought_out:
                    yield ("thought", self.thought_out[-1])
                continue

            if self.state == self.TOOL:
                end = self.buf.find(TOOL_CALL_CLOSE)
                if end == -1:
                    if final and self.buf.strip():
                        tc = self._parse_call(self.buf)
                        if tc:
                            tc.id = f"call_{len(self.calls)}"
                            self.calls.append(tc)
                            yield ("tool_call", tc)
                        self.buf = ""
                        self.state = self.TEXT
                    return
                body = self.buf[:end]
                self.buf = self.buf[end + len(TOOL_CALL_CLOSE):]
                self.state = self.TEXT
                tc = self._parse_call(body)
                if tc:
                    tc.id = f"call_{len(self.calls)}"
                    self.calls.append(tc)
                    yield ("tool_call", tc)
                continue

    @staticmethod
    def _parse_call(body: str) -> ToolCall | None:
        body = body.strip()
        if body.startswith("call:"):
            body = body[len("call:"):]
        m = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*(\{.*)?$", body, re.DOTALL)
        if not m:
            return None
        return ToolCall(name=m.group(1), arguments=parse_arguments(m.group(2) or "{}"),
                        raw=body)

    def _push_thought(self, s: str) -> None:
        s = s.strip()
        if s.startswith("thought"):
            s = s[len("thought"):].strip()
        if s:
            self.thought_out.append(s)

    @staticmethod
    def _clean(s: str) -> str:
        for tok in (TURN_CLOSE, TURN_OPEN, TOOL_RESP_OPEN, TOOL_RESP_CLOSE, CHANNEL_CLOSE):
            s = s.replace(tok, "")
        return s

    @staticmethod
    def _holdback(buf: str) -> int:
        """Length of trailing text that might be the start of a marker."""
        maxlen = min(_MAX_MARKER - 1, len(buf))
        for n in range(maxlen, 0, -1):
            tail = buf[-n:]
            if any(m.startswith(tail) for m in ALL_MARKERS):
                return n
        return 0

    # convenience
    @property
    def text(self) -> str:
        return "".join(self.text_out).strip()

    @property
    def thought(self) -> str:
        return "\n".join(self.thought_out).strip()


def tools_to_schema(tools: list[dict]) -> list[dict]:
    """Normalise tool definitions to the OpenAI-style shape the template wants."""
    out = []
    for t in tools:
        if "function" in t:
            out.append(t)
        else:
            out.append({"type": "function", "function": t})
    return out


# --- incremental prompt rendering -------------------------------------------
def render_user(content: str) -> str:
    return f"{TURN_OPEN}user\n{content}{TURN_CLOSE}\n"


def render_assistant(content: str, tool_calls: list | None = None) -> str:
    """Render an assistant turn. Note: a turn holding tool calls is NOT closed -
    the tool responses continue the same model turn, exactly as the reference
    chat template does."""
    out = f"{TURN_OPEN}model\n{content or ''}"
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", tc)
            name = fn.get("name", "")
            args = fn.get("arguments", {}) or {}
            if isinstance(args, str):
                body = args
            else:
                body = ",".join(f"{k}:{format_argument(v)}"
                                for k, v in sorted(args.items()))
            out += f"{TOOL_CALL_OPEN}call:{name}{{{body}}}{TOOL_CALL_CLOSE}"
        return out
    return out + f"{TURN_CLOSE}\n"


def render_tool_result(name: str, result) -> str:
    return format_tool_response(name, result)


def generation_prompt(think: bool = False, after_tool: bool = False) -> str:
    """The suffix that hands control to the model."""
    if after_tool:
        return ""                       # the model turn is already open
    s = f"{TURN_OPEN}model\n"
    if not think:
        s += f"{CHANNEL_OPEN}thought\n{CHANNEL_CLOSE}"
    return s
