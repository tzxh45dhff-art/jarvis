# The on-device engine

This page is the design record of JARVIS's **local backend**: a 26B-parameter
model (Gemma-4, 4-bit, MLX) running on the Mac itself, with on-device speech.
It is optional. With Azure OpenAI, Gemini or Claude configured, none of this runs
(see the [README](../README.md) and [model routing and cost](model-routing-and-cost.md)).

It needs `pip install -r requirements-local.txt` and about 16 GB of free memory.
Start it with `jarvis --local` or `JARVIS_BACKEND=local`.

The numbers below were measured on an M4 Pro with 24 GB. They are kept because
the reasons behind them (what is slow, why, and what fixed it) apply to any
resident-model design.

## What it runs on

These are the defaults; each is a setting (see `.env.example`).

| Component | Model | Why |
|---|---|---|
| Reasoning + vision | `mlx-community/gemma-4-26b-a4b-it-4bit` | 26B MoE, 4B active. Native tool calling, native vision, 262k context. ~85 tok/s on M4 Pro. |
| Speech out | `mlx-community/Kokoro-82M-bf16` | Warm real-time factor 0.04 (25× realtime). |
| Speech in | `mlx-community/whisper-small.en-mlx` | 4× faster than large-v3-turbo on command-style phrases, slightly more accurate. |
| Wake word | openWakeWord `hey_jarvis` | Local ONNX, ~1 ms per frame. |
| Voice activity | Silero VAD (ships with openWakeWord) | No extra download. |
| Memory search | `all-MiniLM-L6-v2` | 384-d embeddings (also used with cloud backends). |
| Calendar / Reminders | EventKit | ~1 ms per query. AppleScript takes ~9 s. |
| Contacts | Contacts.framework | Cached in memory. AppleScript takes ~17 s. |

One set of model weights serves **both** text and vision. A second copy would
not fit in 24 GB of unified memory alongside everything else.

---

## Architecture

```
  bin/jarvis  ──►  jarvis.cli  ──►  daemon.client ──socket──► daemon.server
                        │                                          │
                   ui.console                                  core.agent
                   voice.*  (mic, wake, TTS)                        │
                                                    ┌───────────────┼───────────────┐
                                                 core.llm      tools.registry   memory.store
                                                    │               │               │
                                            MLX Gemma-4        the tool registry        SQLite + vectors
                                            text + vision      safety-gated    4 memory types
```

**The daemon exists for one reason: the model is 14 GB and takes ~9 s to load.**
Only one engine can exist: startup takes an exclusive `flock`, and both a second
daemon and `--no-daemon` refuse while it is held. Two 14 GB copies do not fit in
24 GB of unified memory, and the failure mode is swapping rather than an error.
Keeping it resident is what makes `jarvis "do the thing"` return in well under a
second instead of paying that on every invocation, and it is what lets wake-word
listening run continuously.

Every seam is a replaceable interface: `LLMBackend`, `TTSBackend`, `STTBackend`,
the VAD, the wake detector, and the tool registry. Swapping the model for a
remote API means implementing `stream()` and nothing else.

### The part that makes it fast

The system prompt plus the tool schemas was ~6,200 tokens when this was measured (it is ~14,000 now, with ~150 tools). Prefilling that costs
about 13 seconds, and an agent loop re-sends a growing history on every step — a
ten-step task would spend minutes just on prefill.

So the KV cache is kept warm and extended in place:

- The static system+tools prefix is prefilled once, at daemon start.
- Each turn renders **incrementally** and feeds only the new tokens.
- The cache cannot be rewound (most layers are `RotatingKVCache` with a 1024
  window), so it is treated as strictly append-only.
- Rendering is done by hand rather than through the chat template, because the
  template strips reasoning channels out of history — which would make the
  re-render disagree with what is physically in the cache.
- `tests/test_prompt_equivalence.py` proves the hand renderer produces byte-identical
  output to the reference template across a full tool-using conversation. If the
  fingerprints ever mismatch, the cache rebuilds from the warm base: a stale
  cache costs latency, never correctness.

Measured on an M4 Pro:

| | before | after |
|---|---|---|
| First token, turn 1 | 5.9 s | **0.34 s** |
| First token, later turns | 5.9 s | **0.12 s** |
| Daemon start, ready to answer | 28 s | **11–20 s** |
| `browser_tabs` | 30.6 s | **0.22 s** |
| `system_status` | 1.4 s | **0.22 s** |
| `now_playing` | 12.3 s | **~0 s** |
| First request after startup | 10.7 s | **0.41 s** |
| First request after 7 min idle | 23.3 s | **1.5 s** |
| Simple question, end to end | ~6 s | **0.5 s** |

The warmed prefix is also written to disk and memory-mapped back on the next
start, so the ~13 s prefill is paid once ever rather than once per launch. The
file is keyed by a hash of the exact prompt text, so changing a tool simply
misses the cache instead of restoring a stale one — which is why the system
prompt carries only the date, never the clock: a minute-resolution timestamp
would invalidate it on every request. The current time rides along with the user
turn instead.

Tools are always offered to the model, even for "what is 25 times 8". Dropping
them looked like a saving until it was measured: the cached prefix *contains*
the tool schemas, so a tools-free prompt is a cache miss costing ~3 s of
re-prefill — far more than the tool call it avoided.

Four more things were only found by timing them:

- **`exists process "X"` takes about six seconds when X is not running.** It
  appears to wait rather than answer. Listing every process once costs ~0.2 s
  and answers any number of membership questions, so `app_running` is a lookup
  into a briefly-cached set and nothing asks the slow question. That alone took
  `now_playing` from 12.3 s to nothing.
- **A restored KV cache is bound to the thread that materialises it.** The
  daemon loads on a background thread and answers on request threads, so
  without an explicit `mx.eval` after loading, the first request dies with
  `There is no Stream(cpu, N) in current thread`.
- **macOS pages out 14 GB of idle weights.** After five quiet minutes the next
  question cost 23 s to fault them back in. Generating a token as a heartbeat
  is *not* enough: this is a mixture-of-experts model, so one token routes
  through about 4 of 128 experts per layer and the other 124 get evicted
  regardless. Reading every parameter — and the 350 MB KV cache, which is
  evicted just as readily — every 60 s is what works, and it costs ~0.02 s while
  everything is resident, so the heartbeat is free until it is needed. Under
  memory pressure from other applications macOS can still evict between ticks,
  and the first question after that pays about a second.
- **The first generation compiles Metal kernels**, which cost the user's first
  question ~10.7 s. The daemon spends that at startup instead, warming through
  the *real* agent path so the shapes it compiles are the ones the first real
  request will use. A less representative warmup only got it to 3.2 s.

Screenshots are downscaled to 1280 px before the vision tower sees them —
measured to give identical OCR accuracy to full Retina resolution at 1.8× the
speed. `see_screen(detail=true)` opts back into high resolution.

### Reasoning is routed, not fixed

Gemma-4 has a thinking channel that the chat template can pre-close. "What is
25 × 8?" runs one forward pass and answers in ~0.3 s. "Research this and write a
paper" enables reasoning and plans. The router is a cheap heuristic on the
request — no extra model call, because that would cost more than it saves.
