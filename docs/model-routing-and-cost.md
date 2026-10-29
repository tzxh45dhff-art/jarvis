# Model routing and cost

With a cloud backend, the cost of JARVIS is set by two things: **which model**
handles a request, and **how often the prompt cache misses**. This page explains
both, how to tune them, and how to see what you are spending.

Everything here is about the Azure OpenAI backend, the one that bills per token
and needs tuning. Gemini's free tier and the on-device model have no per-request
cost; Claude is routed by `JARVIS_CLAUDE_MODEL`.

## What happens to a request

```
 request
   │
   ├─ 1. fast path ─────── a few exact questions the Mac answers itself (time, battery,
   │                       "pause", "volume up"): no model call at all           ~0 ₹
   ├─ 2. saved routine ─── "study mode" expands into its steps
   ├─ 3. router ────────── picks the model for this turn (below)
   ├─ 4. preload ───────── tools the request plainly needs are switched on before step 1
   ├─ 5. agent loop ────── model → tool calls → results → model … (max 40 steps)
   └─ 6. verify ────────── only for multi-step actions that changed something
```

### The router (`jarvis/core/router.py`)

| Route | Setting | Which requests |
|---|---|---|
| main | `JARVIS_AZURE_DEPLOYMENT` | everything else |
| fast | `JARVIS_AZURE_FAST_DEPLOYMENT` | short (≤ 16 words), single-step, no action words: "what's my battery?", "convert 5 miles to km" |
| web | `JARVIS_AZURE_WEB_DEPLOYMENT` | plain English lookups: "who is the CEO of X?", "price of gold today", "weather in Delhi" |
| blender | `JARVIS_AZURE_BLENDER_DEPLOYMENT` | anything that names Blender, or 3D work with Blender in front; also switches mid-turn when the model reaches for `blender_script` |
| search | `JARVIS_AZURE_SEARCH_DEPLOYMENT` | the grounded web-search call inside `web_answer` |
| background | `JARVIS_AZURE_BACKGROUND_DEPLOYMENT` | learning facts about you from the conversation |

Unset routes use the main model. The rules are deliberately conservative:

- Anything that **acts** (open, click, type, write, create, send, download, delete,
  play, schedule…) or chains steps (`then`, `and also`, `;`) stays on the main model.
- Requests about *you or this Mac* ("my battery", "my calendar") are not web lookups.
- **Romanised Hindi and non-Latin scripts stay on the main model.** The small model
  answered Hinglish in Devanagari (the voice cannot speak it) and read "kal"
  (tomorrow/yesterday) as today.
- A request that starts on the fast or web model **hands over to the main model**
  the moment a step fails, the model repeats itself, or step 4 is reached.

`JARVIS_ROUTER=0` turns the router off. `tests/test_speed_paths.py` pins the rules.

## The prompt cache is the bill

Every request carries a fixed prefix: the system prompt plus every tool schema,
about **14,000 tokens**. Azure bills input tokens in three classes:

| Class | Price (× input) |
|---|---|
| plain input | 1.00 |
| cache **write** (the prefix stored for the first time, or again after a miss) | 1.25 |
| cache **read** (a hit) | 0.10 |

A cold request therefore costs about **12×** a warm one (measured: ₹3.31 vs ₹0.27
on a ₹192 / 1M-token model). Measured over a week of real use, cache writes were
the largest line on the invoice, ahead of output tokens. What kept causing misses,
and what was done:

1. **Two APIs, two caches.** `chat/completions` and the Responses API do not share
   a cache. Some models reject "reasoning + tools" on chat/completions, so steps that
   needed thinking went to Responses and re-stored the whole prefix. Now every agent
   step uses the Responses API (`reasoning.effort` = `none`, or `low` when thinking).
   Switch back with `JARVIS_AZURE_API=chat`.
2. **A fixed `prompt_cache_key`** per model sends all requests to one cache partition.
3. **`prompt_cache_retention: 24h`** (`JARVIS_AZURE_CACHE_RETENTION=24h`) keeps the
   prefix alive across idle gaps, so the first request after a break is a read.
4. **The tool list never changes mid-turn.** Enabling a deferred tool changes the
   prefix and re-stores it. Tools a request plainly needs (Blender, video editing,
   archives, transcription…) are switched on *before* the first step
   (`tools/toolsearch.py: preload_for`), instead of through an extra `find_tools` step.
5. **Calls that used to drop the tool list** (the "stuck job" summary, the background
   learner) now send it, or run on a different model entirely.
6. **Old tool results are shrunk** when history is trimmed: a whole web page read for a
   question answered ten requests ago is cut to its first 1,500 characters.

Quirk worth knowing: a cache written by a request with reasoning **on** cannot be
read by a later request with reasoning **off**; the reverse works.

**Rule of thumb:** any edit to the system prompt or the tool list costs one cache
write (~₹3) on the next request. Batch such edits.

## Seeing what you spend

Every Azure request is appended to `~/.jarvis/data/usage.jsonl` (model, kind of call,
token counts, cost) and summarised by:

```bash
jarvis cost         # today
jarvis cost 7       # the last 7 days
```

```
Model cost for today (24 h): ₹18.78 over 21 requests
  prompt cache: 72% of input tokens were cache hits; 26,484 tokens written to the cache
  by model: gpt-6-sol ₹16.60 (15), gpt-6-luna ₹2.18 (6)
  by kind: web_answer ₹11.37 (8), think ₹4.23 (4), step ₹3.18 (9)
```

Kinds: `step` (an agent step), `think` (a step with reasoning), `web_answer` (grounded
search, including the per-search fee), `vision` (a screenshot or image question).

Prices live in `jarvis/core/ledger.py` (`PRICES`, ₹ per million tokens: input,
cached, cache-write, output; Azure retail, Global Standard, October 2026). They are
a table you can edit; an unknown model is priced like the main one. Azure's own cost
view lags about a day and cannot say which request was expensive; the ledger can.

## Web search

`web_answer` is one request to the model's built-in web search: it searches, reads
and cites. Measured on a lookup, the small model gave the same facts as the large
one for about a quarter of the cost, most of it Azure's flat per-search fee (about
₹0.48). It is routed by `JARVIS_AZURE_SEARCH_DEPLOYMENT`; set it to a small model.

## Typical numbers

Measured on Azure Global Standard, October 2026, warm cache:

| Request | Model route | Cost |
|---|---|---|
| "what's my battery?" | fast path, no model | ~₹0 |
| "weather in Delhi right now" | fast | ~₹0.04 |
| "who is the CEO of Nvidia?" | web (search + two small-model steps) | ~₹0.7 |
| one agent step on the main model | main | ₹0.4–0.5 |
| "make a one-page Word document…" | main, 3 steps | ~₹5 |
