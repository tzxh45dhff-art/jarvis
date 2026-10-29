"""
LLM abstraction.

`LLMBackend` is the seam: swap in a different model (remote API, another local
model) by implementing `stream` / `stream_vision` without touching the agent.

`MLXGemmaBackend` runs mlx-community/gemma-4-26b-a4b-it-4bit locally. One
weights instance serves *both* text and vision, which matters on a 24 GB
machine where a second copy would not fit.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol

from ..config import CONFIG
from . import protocol as P

log = logging.getLogger("jarvis.llm")


def _is_cached(repo: str) -> bool:
    """True when a HuggingFace repo already has weights on disk."""
    from pathlib import Path
    if Path(repo).expanduser().exists():
        return True
    root = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    d = root / "hub" / ("models--" + repo.replace("/", "--"))
    return d.exists() and any(d.rglob("*.safetensors"))


@dataclass
class Delta:
    """One streaming increment."""
    text: str = ""
    kind: str = "text"          # text | thought | tool_call | done
    tool_call: Any = None
    tps: float = 0.0
    prompt_tps: float = 0.0
    n_tokens: int = 0


@dataclass
class Completion:
    text: str = ""
    thought: str = ""
    tool_calls: list[P.ToolCall] = field(default_factory=list)
    tps: float = 0.0
    prompt_tps: float = 0.0
    ttft: float = 0.0
    n_tokens: int = 0
    truncated: bool = False


class LLMBackend(Protocol):
    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               think: bool = False, max_tokens: int | None = None,
               temperature: float | None = None,
               stop_event: threading.Event | None = None) -> Iterator[Delta]: ...

    def count_tokens(self, text: str) -> int: ...


class _TextAdapter:
    """
    mlx-vlm's language sub-model returns a `LanguageModelOutput`; mlx-lm's
    generate loop expects raw logits. This proxy bridges the two so a single
    set of weights can drive both paths.
    """
    __slots__ = ("_inner",)

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __call__(self, *a, **kw):
        out = self._inner(*a, **kw)
        return getattr(out, "logits", out)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)


class MLXGemmaBackend:
    """Local Gemma-4 MoE on Apple Silicon via MLX."""

    def __init__(self, model_id: str | None = None):
        self.model_id = model_id or CONFIG.llm.model
        self._model = None
        self._processor = None
        self._tokenizer = None
        self._text_model = None
        self._config = None
        self._lock = threading.RLock()
        self._loaded = threading.Event()
        self._cache = None
        self._cache_tokens: list[int] = []
        self._base_tokens: list[int] = []
        self._folded = 0
        self._sig: list = []
        self._warm_done = False
        self._cache_tail = "block"

    # -- lifecycle ---------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            # Skip the per-start revision check when the weights are already
            # cached: it is a network round trip that buys nothing and makes
            # startup fail without a connection. Only when they are cached -
            # a first run must still be allowed to download.
            if _is_cached(self.model_id):
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            t0 = time.time()
            from mlx_vlm import load as vlm_load
            from mlx_vlm.utils import load_config
            from mlx_lm.tokenizer_utils import TokenizerWrapper

            self._model, self._processor = vlm_load(self.model_id)
            self._config = load_config(self.model_id)
            raw_tok = getattr(self._processor, "tokenizer", self._processor)
            # mlx-lm needs the extended EOS set (<turn|>, <|tool_response>, <eos>)
            self._tokenizer = TokenizerWrapper(raw_tok, eos_token_ids=P.EOS_TOKEN_IDS)
            self._text_model = _TextAdapter(
                getattr(self._model, "language_model", self._model)
            )
            self._reset_cache()
            self._loaded.set()
            log.info("model loaded in %.1fs", time.time() - t0)

    @property
    def ready(self) -> bool:
        return self._model is not None

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._loaded.wait(timeout)

    # -- prompting ---------------------------------------------------------
    def build_prompt(self, messages: list[dict], tools: list[dict] | None,
                     think: bool) -> str:
        self.load()
        msgs = self._normalise(messages)
        kw: dict[str, Any] = {"add_generation_prompt": True, "tokenize": False,
                              "enable_thinking": bool(think)}
        if tools:
            kw["tools"] = P.tools_to_schema(tools)
        return self._tokenizer.apply_chat_template(msgs, **kw)

    @staticmethod
    def _normalise(messages: list[dict]) -> list[dict]:
        """Convert our internal message shape to what the chat template wants."""
        out = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                # The template resolves the tool's name from `tool_call_id`, and
                # falls back to the explicit `name` field - dropping it makes the
                # template render None and raise.
                out.append({"role": "tool",
                            "tool_call_id": m.get("tool_call_id", ""),
                            "name": m.get("name") or "tool",
                            "content": m.get("content", "")})
            elif role == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": m.get("content") or "",
                            "tool_calls": m["tool_calls"]})
            else:
                out.append({"role": role, "content": m.get("content", "")})
        return out

    def count_tokens(self, text: str) -> int:
        self.load()
        return len(self._tokenizer.encode(text))

    # -- generation --------------------------------------------------------
    #
    # Prompt caching. The system+tools preamble is ~3.7k tokens and costs ~6 s
    # to prefill; an agent loop re-sends a growing history on every step, so
    # without reuse a ten-step task would spend a minute on prefill alone.
    #
    # The KV cache cannot be rewound (most layers are RotatingKVCache with a
    # 1024 window, and trimming below the window is impossible), so the cache is
    # treated as append-only. We therefore render each new turn incrementally
    # instead of re-rendering the whole conversation through the chat template -
    # the template strips reasoning channels from history, which would make the
    # re-render disagree with what is physically in the cache.
    #
    # `_folded` tracks how many messages the cache already represents, and
    # `_sig` fingerprints them. Any mismatch rebuilds from the warm base, so a
    # stale cache degrades latency, never correctness.

    def _reset_cache(self):
        from mlx_lm.models.cache import make_prompt_cache
        self._cache = make_prompt_cache(self._text_model)
        self._cache_tokens = []
        self._folded = 0
        self._sig = []
        self._pending_assistant = False

    @staticmethod
    def _msg_sig(m: dict) -> str:
        import hashlib
        raw = f"{m.get('role')}|{m.get('content')}|{m.get('tool_call_id','')}|" \
              f"{m.get('name','')}|{m.get('tool_calls','')}"
        return hashlib.blake2b(raw.encode(), digest_size=8).hexdigest()

    def base_prefix_text(self) -> str:
        from ..tools.registry import REGISTRY
        from . import prompts
        return self._tokenizer.apply_chat_template(
            [{"role": "system", "content": prompts.system_prompt()}],
            add_generation_prompt=False, tokenize=False, enable_thinking=False,
            tools=P.tools_to_schema(REGISTRY.schemas()),
        )

    def _prefix_cache_path(self, text: str):
        """One file per (model, system prompt, tool set) combination."""
        import hashlib
        from ..config import DATA_DIR
        d = DATA_DIR / "prefix_cache"
        d.mkdir(parents=True, exist_ok=True)
        key = hashlib.blake2b(
            (self.model_id + "\x00" + text).encode(), digest_size=12).hexdigest()
        return d / f"{key}.safetensors"

    def warm(self, use_disk: bool = True) -> float:
        """
        Make the static system+tools prefix resident in the KV cache.

        Prefilling ~6k tokens costs about 11 s. The result is deterministic, so
        it is written to disk and memory-mapped back on the next start - which
        turns that 11 s into roughly nothing. The file is keyed by a hash of the
        exact prompt text, so changing a tool or the system prompt simply misses
        the cache rather than restoring a stale one.
        """
        self.load()
        t0 = time.time()
        text = self.base_prefix_text()
        toks = self._tokenizer.encode(text)
        path = self._prefix_cache_path(text)

        with self._lock:
            self._reset_cache()
            loaded = False
            if use_disk and path.exists():
                try:
                    import mlx.core as mx
                    from mlx_lm.models.cache import load_prompt_cache
                    cache, meta = load_prompt_cache(str(path), return_metadata=True)
                    if int(meta.get("n_tokens", -1)) == len(toks):
                        # A restored cache is lazy, and its arrays stay bound to
                        # the stream of whichever thread materialises them. The
                        # daemon loads on a background thread and generates on
                        # request threads, so without forcing evaluation here the
                        # first request dies with
                        # "There is no Stream(cpu, N) in current thread".
                        mx.eval([c.state for c in cache])
                        self._cache = cache
                        self._cache_tokens = list(toks)
                        loaded = True
                        log.info("prefix cache restored from disk (%d tokens)",
                                 len(toks))
                except Exception as e:
                    log.warning("prefix cache load failed (%s); rebuilding", e)
                    self._reset_cache()
            if not loaded:
                self._prefill(toks)
                if use_disk:
                    try:
                        from mlx_lm.models.cache import save_prompt_cache
                        save_prompt_cache(str(path), self._cache,
                                          metadata={"n_tokens": str(len(toks)),
                                                    "model": self.model_id})
                        self._prune_old_caches(path)
                    except Exception as e:
                        log.warning("could not persist prefix cache: %s", e)
            self._base_tokens = list(toks)
            self._folded = 1
            self._sig = []
            self._cache_tail = "block"
            self._warm_done = True
        return time.time() - t0

    @staticmethod
    def _prune_old_caches(keep) -> None:
        """Keep only the current prefix cache; each one is a few hundred MB."""
        try:
            for f in keep.parent.glob("*.safetensors"):
                if f != keep:
                    f.unlink(missing_ok=True)
        except Exception:
            pass

    def _prefill(self, toks: list[int]) -> None:
        import mlx.core as mx
        step = 512
        for i in range(0, len(toks), step):
            self._text_model(mx.array(toks[i:i + step])[None], cache=self._cache)
            mx.eval([c.state for c in self._cache])
        self._cache_tokens = list(self._cache_tokens) + list(toks)

    def _render_delta(self, messages: list[dict], think: bool) -> str:
        """
        Wire text for messages[self._folded:] plus the generation prompt.

        `_cache_tail` records how the cached text ends, because that decides two
        things the chat template would otherwise handle:
          * "turn_open"  - generation stopped on <turn|> with no trailing
                           newline, so the next turn opener needs one.
          * "tool_open"  - generation stopped on <|tool_response>, so the first
                           tool result must not repeat that opening marker.
          * "block"      - the cache ends on a fully rendered block.
        """
        out: list[str] = []
        tail = self._cache_tail
        first = True
        # True while a model turn is still open (tool calls / results continue it)
        model_turn_open = (tail == "tool_open")

        for m in messages[self._folded:]:
            role = m.get("role")
            if role == "system":
                continue
            if role == "user":
                prefix = "\n" if (first and tail == "turn_open") else ""
                out.append(prefix + P.render_user(m.get("content") or ""))
                model_turn_open = False
            elif role == "assistant":
                prefix = "\n" if (first and tail == "turn_open") else ""
                out.append(prefix + P.render_assistant(m.get("content") or "",
                                                       m.get("tool_calls")))
                model_turn_open = bool(m.get("tool_calls"))
            elif role == "tool":
                # Pass the payload through as a string, exactly as the
                # reference template does, so both rendering paths agree.
                block = P.render_tool_result(m.get("name") or "tool",
                                             m.get("content", ""))
                if first and tail == "tool_open":
                    # generation already emitted the opening <|tool_response>
                    block = block[len(P.TOOL_RESP_OPEN):]
                out.append(block)
                model_turn_open = True
            first = False

        out.append(P.generation_prompt(think=think, after_tool=model_turn_open))
        return "".join(out)

    def _can_extend(self, messages: list[dict]) -> bool:
        """True when the cache is a valid prefix of this conversation."""
        if not getattr(self, "_warm_done", False) or self._cache is None:
            return False
        if not messages or messages[0].get("role") != "system":
            return False
        body = messages[1:]
        folded_body = self._folded - 1
        if folded_body < 0 or folded_body > len(body):
            return False
        if len(self._sig) != folded_body:
            return False
        for s, m in zip(self._sig, body[:folded_body]):
            if s is None:
                # placeholder for the assistant turn the model just generated;
                # its text is already physically in the cache
                if m.get("role") != "assistant":
                    return False
                continue
            if s != self._msg_sig(m):
                return False
        return True

    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               think: bool = False, max_tokens: int | None = None,
               temperature: float | None = None,
               stop_event: threading.Event | None = None) -> Iterator[Delta]:
        self.load()
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        temp = (CONFIG.llm.tool_temperature if tools
                else CONFIG.llm.temperature) if temperature is None else temperature
        sampler = make_sampler(temp=temp, top_p=0.95 if temp > 0 else 1.0)
        sp = P.StreamParser()
        last = None
        n = 0

        with self._lock:
            incremental = (not think) and self._can_extend(messages)
            if incremental:
                delta = self._render_delta(messages, think)
                feed = self._tokenizer.encode(delta, add_special_tokens=False)
                if not feed:
                    incremental = False
            if not incremental:
                if getattr(self, "_warm_done", False) and not think:
                    # rebuild from the warm base rather than from nothing
                    self._reset_cache()
                    self._prefill(self._base_tokens)
                    self._folded = 1
                    self._sig = []
                    self._cache_tail = "block"
                    delta = self._render_delta(messages, think)
                    feed = self._tokenizer.encode(delta, add_special_tokens=False)
                else:
                    self._reset_cache()
                    prompt = self.build_prompt(messages, tools, think)
                    feed = self._tokenizer.encode(prompt)
                    self._folded = 0
                    self._sig = []

            kw = {"prompt_cache": self._cache} if not think else {}
            if think:
                self._reset_cache()

            gen_tokens: list[int] = []
            for r in stream_generate(self._text_model, self._tokenizer, prompt=feed,
                                     max_tokens=max_tokens or CONFIG.llm.max_tokens,
                                     sampler=sampler, **kw):
                if stop_event is not None and stop_event.is_set():
                    break
                last, n = r, n + 1
                tk = getattr(r, "token", None)
                if tk is not None:
                    gen_tokens.append(int(tk))
                for kind, payload in sp.feed(r.text):
                    if kind == "text":
                        yield Delta(text=payload, kind="text")
                    elif kind == "thought":
                        yield Delta(text=payload, kind="thought")
                    elif kind == "tool_call":
                        yield Delta(kind="tool_call", tool_call=payload)
            for kind, payload in sp.close():
                if kind == "text":
                    yield Delta(text=payload, kind="text")
                elif kind == "thought":
                    yield Delta(text=payload, kind="thought")
                elif kind == "tool_call":
                    yield Delta(kind="tool_call", tool_call=payload)

            if think:
                # A thinking turn puts a marker inside the system block, so its
                # KV cannot be shared. Drop the cache but keep the warm base so
                # the next (non-thinking) turn rebuilds from it cheaply.
                base = self._base_tokens
                self._reset_cache()
                self._base_tokens = base
                self._cache = None
            else:
                self._cache_tokens += list(feed) + gen_tokens
                # every supplied message is now represented, plus the assistant
                # turn the model just produced (which the caller will append).
                self._folded = len(messages) + 1
                self._sig = [self._msg_sig(m) for m in messages[1:]]
                # How did generation end? That determines how the next delta
                # must be joined onto the cached text.
                last_tok = gen_tokens[-1] if gen_tokens else None
                if last_tok == 50:              # <|tool_response>
                    self._cache_tail = "tool_open"
                elif last_tok == 106:           # <turn|>
                    self._cache_tail = "turn_open"
                else:
                    self._cache_tail = "turn_open"
                # the caller appends exactly one assistant message next
                self._sig.append(None)   # placeholder, reconciled on next call

        yield Delta(kind="done",
                    tps=getattr(last, "generation_tps", 0.0) or 0.0,
                    prompt_tps=getattr(last, "prompt_tps", 0.0) or 0.0,
                    n_tokens=n)

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 think: bool = False, max_tokens: int | None = None,
                 temperature: float | None = None,
                 stop_event: threading.Event | None = None) -> Completion:
        """Non-streaming convenience wrapper around :meth:`stream`."""
        c = Completion()
        parts: list[str] = []
        thoughts: list[str] = []
        t0 = time.time()
        for d in self.stream(messages, tools, think, max_tokens, temperature, stop_event):
            if d.kind == "text":
                if not parts:
                    c.ttft = time.time() - t0
                parts.append(d.text)
            elif d.kind == "thought":
                thoughts.append(d.text)
            elif d.kind == "tool_call":
                c.tool_calls.append(d.tool_call)
            elif d.kind == "done":
                c.tps, c.prompt_tps, c.n_tokens = d.tps, d.prompt_tps, d.n_tokens
        c.text = "".join(parts).strip()
        c.thought = "\n".join(thoughts).strip()
        return c

    # -- vision ------------------------------------------------------------
    def describe_images(self, prompt: str, images: list[str],
                        max_tokens: int = 700) -> str:
        """Run the vision tower. Same weights, no second copy in memory."""
        self.load()
        from mlx_vlm import generate as vlm_generate
        from mlx_vlm.prompt_utils import apply_chat_template
        with self._lock:
            formatted = apply_chat_template(
                self._processor, self._config, prompt, num_images=len(images)
            )
            out = vlm_generate(
                self._model, self._processor, formatted, image=images,
                max_tokens=max_tokens, temperature=0.0, verbose=False,
            )
        text = getattr(out, "text", out)
        return P.parse_output(str(text)).text or str(text)

    def touch_weights(self, cancel: "threading.Event | None" = None) -> float:
        """
        Read every parameter so macOS keeps the weights resident.

        A generation is not enough: this is a mixture-of-experts model, so one
        token routes through roughly 4 of 128 experts per layer and the other
        124 get evicted anyway.

        `cancel` is checked between arrays and aborts immediately. Under real
        memory pressure a full pass can take over a minute, and a background
        warm-up must never make a user wait - it yields the moment a request
        arrives.
        """
        import mlx.core as mx
        from mlx.utils import tree_flatten
        t0 = time.time()
        if cancel is not None and cancel.is_set():
            return 0.0
        with self._lock:
            if self._model is None:
                return 0.0
            total = mx.zeros((), dtype=mx.float32)
            pending = []

            def touch(arr):
                if isinstance(arr, mx.array) and arr.size:
                    # stride ~1 page apart: pages in the whole buffer without
                    # materialising a copy of it
                    pending.append(arr.reshape(-1)[::997].astype(mx.float32).sum())

            arrays = [p for _, p in tree_flatten(self._model.parameters())]
            for c in (self._cache or []):
                st = getattr(c, "state", None)
                arrays += list(st) if isinstance(st, (list, tuple)) else [st]

            # Evaluate in batches so a cancel lands quickly instead of after the
            # whole 14 GB.
            for i, a in enumerate(arrays):
                if cancel is not None and cancel.is_set():
                    break
                touch(a)
                if len(pending) >= 24:
                    mx.eval(pending)
                    pending = []
            if pending:
                mx.eval(pending)
        return time.time() - t0

    def unload(self) -> None:
        with self._lock:
            self._model = self._processor = self._tokenizer = None
            self._text_model = None
            self._cache = None
            self._cache_tokens = []
            self._loaded.clear()
            try:
                import mlx.core as mx
                mx.clear_cache()
            except Exception:
                pass


_BACKEND = None


#: Backends the user can ask for, by any of their aliases.
BACKENDS = {
    "claude": ("claude", "anthropic", "api"),
    "local": ("local", "gemma", "mlx", "offline"),
    "cloud": ("cloud", "gemini", "google"),
    "azure": ("azure", "luna", "gpt", "openai"),
}


def resolve_kind(name: str) -> str:
    name = (name or "").strip().lower()
    for canon, aliases in BACKENDS.items():
        if name in aliases:
            return canon
    return ""


def default_kind() -> str:
    """
    Gemini when its key is configured, the local model otherwise.

    Gemini (gemini-3.5-flash-lite) picks and chains tools more reliably than the
    local model and answers in about a second, for roughly a tenth of a cent a
    step. The local model stays one command away - `jarvis --local` or
    `/backend local` - and is what runs if the key is missing or removed.
    JARVIS_BACKEND overrides both.
    """
    env = resolve_kind(os.environ.get("JARVIS_BACKEND", ""))
    if env:
        return env
    try:
        # Azure (GPT-6 Luna on the user's student credits) when configured:
        # no daily cap, ~1 s tool calls, and no key stored anywhere.
        from .azure import AzureBackend
        if AzureBackend().available:
            return "azure"
    except Exception:
        pass
    try:
        from .cloud import api_key
        if api_key():
            return "cloud"
    except Exception:
        pass
    return "local"


def backend_name(b) -> str:
    """Human-readable label for whichever backend is active."""
    kind = getattr(b, "_kind", "local")
    if kind == "claude":
        return getattr(b, "model_id", "claude")
    if kind == "cloud":
        return getattr(b, "model_id", "") or "gemini"
    if kind == "azure":
        return f"{getattr(b, 'model_id', 'gpt')} (Azure)"
    return "gemma-4 26B local"


def is_remote(b) -> bool:
    """True when the backend needs no local weights (so no 14 GB warmup)."""
    return getattr(b, "_kind", "local") in ("claude", "cloud", "azure")


def get_backend(kind: str = ""):
    """
    The active reasoning backend.

    Switch with `jarvis --local` / `--claude` / `--cloud`, the `/backend`
    command inside the shell, or JARVIS_BACKEND in the environment.
    """
    global _BACKEND
    want = resolve_kind(kind) or default_kind()
    if _BACKEND is not None and getattr(_BACKEND, "_kind", "") == want:
        return _BACKEND

    if want == "azure":
        from .azure import AzureBackend
        b = AzureBackend()
        if b.available:
            b._kind = "azure"
            _BACKEND = b
            return _BACKEND
        log.warning("azure backend requested but not configured; falling back")
        want = "cloud"
    if want == "claude":
        from .anthropic import AnthropicBackend
        b = AnthropicBackend()
        if b.available:
            b._kind = "claude"
            _BACKEND = b
            return _BACKEND
        log.warning("claude backend requested but no ANTHROPIC_API_KEY found; "
                    "falling back to the local model")
    elif want == "cloud":
        from .cloud import GeminiBackend
        b = GeminiBackend()
        if b.available:
            b._kind = "cloud"
            _BACKEND = b
            return _BACKEND
        log.warning("cloud backend requested but no API key found; using local")

    if _BACKEND is None or getattr(_BACKEND, "_kind", "") != "local":
        _BACKEND = MLXGemmaBackend()
        _BACKEND._kind = "local"
    return _BACKEND
