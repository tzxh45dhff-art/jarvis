"""
Fast, accurate answers from the internet.

Measured on this Mac (October 2026), for a factual question:

    search (ddgs cascade) 1.2-2.0 s  ->  model reads snippets  ->  fetch 1-2 s
    ->  model reads page  ->  answer                              ~10-12 s, 4 calls

    web_answer (grounded search, one call)                        ~3.5-4 s

`web_answer` asks the Azure deployment's built-in web search to find, read and
cite in a single request. When that is unavailable (another backend, no network
to Azure, the tool refused) it falls back to an evidence pack built here:
search, fetch the best few pages in parallel, and return only the passages that
actually bear on the question - so the model reads 3 KB of relevant text
instead of three whole articles.

A quick plain-HTTP Bing scrape was tried and rejected: 0.5 s, but it returned
dictionary pages for "who won the 2024 Nobel prize in physics" from this
network. Fast and wrong is worse than slow.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import math
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

from ..safety.policy import Risk
from . import web as W
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.research")

_ANSWERS = W.TTLCache(96)
_UNSUPPORTED_UNTIL = 0.0           # grounded search refused: skip it for a while
_COUNTRY: str | None = None

INSTRUCTIONS = (
    "You answer questions using web search. Be precise and brief: lead with the "
    "answer, then only the detail that matters. For anything that changes (prices, "
    "scores, versions, schedules, office holders) state the date or time the "
    "figure is from. If reliable sources disagree, say so and give both. If you "
    "could not find it, say that - never guess or fill in from memory. Use metric "
    "units (°C, km, kg) and the user's local currency unless asked otherwise."
)


# ---------------------------------------------------------------- helpers

def _country() -> str:
    """Two-letter region from the Mac's own settings, for local results."""
    global _COUNTRY
    if _COUNTRY is None:
        code = "US"
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLocale"],
                                 capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"_([A-Z]{2})", out)
            if m:
                code = m.group(1)
        except Exception:
            pass
        _COUNTRY = code
    return _COUNTRY


def _key(question: str, depth: str) -> tuple:
    q = re.sub(r"[^\w\s]", "", (question or "").lower())
    return (re.sub(r"\s+", " ", q).strip(), depth)


_INLINE_CITE = re.compile(r"\s*\(\[[^\]]+\]\([^)]+\)\)")
_BARE_CITE = re.compile(r"\s*\[\[?\d+\]?\]")


# Some models mark citations with private-use characters around "cite" and
# "turn0search0"-style ids; the characters are invisible, the ids are not.
_PUA = "\ue000-\uf8ff"
_MODEL_CITE = re.compile(rf"[{_PUA}]*cite(?:[{_PUA}]*turn\d+[a-z]+\d+)+[{_PUA}]*")


def _clean_answer(text: str) -> str:
    t = _MODEL_CITE.sub("", text or "")
    t = re.sub(rf"[{_PUA}]", "", t)
    t = _INLINE_CITE.sub("", t)
    t = _BARE_CITE.sub("", t)
    return re.sub(r"[ \t]+\n", "\n", t).strip()


def _parse_response(j: dict) -> tuple[str, list[dict]]:
    """(answer text, unique cited sources) from a Responses API payload."""
    text, sources, seen = "", [], set()
    for item in j.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if part.get("type") != "output_text":
                continue
            text += part.get("text", "")
            for a in part.get("annotations", []) or []:
                if a.get("type") != "url_citation":
                    continue
                url = a.get("url") or ""
                key = W._norm_url(url)
                if url and key not in seen:
                    seen.add(key)
                    sources.append({"title": W._clean(a.get("title")) or
                                    urllib.parse.urlsplit(url).netloc,
                                    "url": url})
    return _clean_answer(text), sources


# ---------------------------------------------------------------- grounded search

def _grounded(question: str, depth: str) -> dict | None:
    """One Azure request that searches, reads and cites. None if unavailable."""
    global _UNSUPPORTED_UNTIL
    if time.time() < _UNSUPPORTED_UNTIL:
        return None
    try:
        from ..core import azure as A
        s = A.settings()
        if not (s.get("endpoint") and A._az()):
            return None
        # Grounded search reads search results and writes a short sourced
        # answer; the main model then words the reply. GPT-6 Luna gives the same
        # facts as Sol for a fraction of the cost (measured 19-20 Oct 2026:
        # quick Rs 0.53 vs 1.5-3.3, thorough Rs 1.1 vs 2.6-5.5, most of Luna's
        # being the Rs 0.48-per-search fee), so `search_deployment` handles both.
        deployment = s.get("search_deployment") or s.get("deployment")
        if not deployment:
            return None
        token = A._TOKEN.get()
        endpoint = s["endpoint"].rstrip("/") + "/openai/v1/responses"
    except Exception as e:
        log.info("grounded search unavailable: %s", e)
        return None

    quick = depth != "thorough"
    body = {
        "model": deployment, "input": question, "instructions": INSTRUCTIONS,
        "max_output_tokens": 700 if quick else 1500,
        "tools": [{"type": "web_search",
                   "search_context_size": "low" if quick else "medium",
                   "user_location": {"type": "approximate", "country": _country()}}],
        "tool_choice": "required",
        "reasoning": {"effort": "none" if quick else "low"},
    }
    for attempt in range(3):
        req = urllib.request.Request(
            endpoint, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        try:
            from ..core import http_pool
            # A grounded call that has not answered by now is stuck: six real
            # calls sat the full 60 s before the 3-6 s fallback took over.
            # Answers that worked came back in 4-44 s.
            with http_pool.urlopen(req, timeout=40 if quick else 60) as r:
                j = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", "replace")
            if e.code == 429 and attempt < 2:
                time.sleep(min(float(e.headers.get("Retry-After") or 2), 6))
                continue
            if e.code == 400 and "effort" in detail and body["reasoning"]["effort"] == "none":
                body["reasoning"] = {"effort": "low"}       # this model wants some
                continue
            if e.code == 400 and "tool_choice" in detail and "tool_choice" in body:
                body.pop("tool_choice")
                continue
            if e.code in (400, 404):
                _UNSUPPORTED_UNTIL = time.time() + 600
            log.warning("grounded search HTTP %s: %s", e.code, detail[:160])
            return None
        except Exception as e:
            log.warning("grounded search failed: %s", e)
            return None
    else:
        return None

    try:
        from ..core import ledger
        u = j.get("usage") or {}
        det = u.get("input_tokens_details") or {}
        ledger.record(deployment, "web_answer", int(u.get("input_tokens", 0) or 0),
                      int(det.get("cached_tokens", 0) or 0),
                      int(det.get("cache_write_tokens", 0) or 0),
                      int(u.get("output_tokens", 0) or 0),
                      searches=sum(1 for it in j.get("output", []) or []
                                   if it.get("type") == "web_search_call"))
    except Exception:
        pass
    answer, sources = _parse_response(j)
    if not answer:
        return None
    return {"answer": answer, "sources": sources[:8]}


# ---------------------------------------------------------------- evidence pack

_SKIP_DOMAINS = ("youtube.com", "youtu.be", "twitter.com", "x.com", "facebook.com",
                 "instagram.com", "tiktok.com", "linkedin.com", "pinterest.com")
_PRIOR = {
    "wikipedia.org": 1.0, "britannica.com": 0.8, "github.com": 0.8,
    "stackoverflow.com": 0.8, "developer.mozilla.org": 0.9, "docs.python.org": 0.9,
    "nature.com": 0.9, "arxiv.org": 0.8, "reuters.com": 0.8, "apnews.com": 0.8,
    "bbc.com": 0.75, "bbc.co.uk": 0.75, "nih.gov": 0.9, "who.int": 0.9,
    "reddit.com": 0.45, "quora.com": 0.3, "medium.com": 0.5,
}
_STOP = set("a an the of to in on at for and or is are was were be been it its "
            "this that with as by from what who which when where why how do does "
            "did can could should would about into than then there their".split())


def _domain(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host.removeprefix("www.").removeprefix("m.")


def _prior(url: str) -> float:
    d = _domain(url)
    for k, v in _PRIOR.items():
        if d == k or d.endswith("." + k):
            return v
    if d.endswith((".gov", ".edu", ".gov.in", ".ac.uk", ".gov.uk")):
        return 0.9
    if d.startswith(("docs.", "developer.", "support.", "help.")):
        return 0.85
    return 0.6


def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-\.]*", (text or "").lower())
            if w not in _STOP and len(w) > 1]


def passages(text: str, size: int = 520) -> list[str]:
    """Paragraph-sized pieces, small ones merged, big ones split on sentences."""
    out: list[str] = []
    cur = ""
    for para in re.split(r"\n+", text or ""):
        para = para.strip()
        if not para:
            continue
        if len(para) > size * 1.6:
            sents = re.split(r"(?<=[.!?])\s+", para)
            buf = ""
            for s in sents:
                if len(buf) + len(s) > size and buf:
                    out.append(buf.strip())
                    buf = ""
                buf += " " + s
            para = buf.strip()
        if len(cur) + len(para) < size * 0.6:
            cur = (cur + " " + para).strip()
        else:
            if cur:
                out.append(cur)
            cur = para
    if cur:
        out.append(cur)
    return [p for p in out if len(p) > 40]


def rank_passages(question: str, docs: list[tuple[int, str]],
                  top: int = 8, per_doc: int = 3) -> list[tuple[int, str, float]]:
    """BM25-style relevance of every passage to the question, across all
    documents together so common words weigh little. [(doc_index, text, score)]"""
    q = _terms(question)
    if not q:
        return [(d, t, 0.0) for d, t in docs[:top]]
    toks = [Counter(_terms(t)) for _, t in docs]
    n = max(1, len(docs))
    df = Counter()
    for c in toks:
        for w in set(c):
            df[w] += 1
    avg = sum(sum(c.values()) for c in toks) / n or 1.0
    k1, b = 1.4, 0.75
    scored = []
    for (di, text), c in zip(docs, toks):
        dl = sum(c.values()) or 1
        score = 0.0
        for w in set(q):
            f = c.get(w, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avg))
        # reward passages that carry the question's rarer terms together
        hit = sum(1 for w in set(q) if c.get(w))
        score *= 1 + 0.35 * (hit / len(set(q)))
        if re.search(r"\d", text) and re.search(r"\b(when|how many|how much|price|"
                                                r"year|date|population|cost)\b",
                                                question, re.I):
            score *= 1.15                    # figures matter for those questions
        scored.append((di, text, score))
    scored.sort(key=lambda x: -x[2])
    out, per = [], Counter()
    for di, text, sc in scored:
        if sc <= 0:
            break
        if per[di] >= per_doc:
            continue
        per[di] += 1
        out.append((di, text, round(sc, 2)))
        if len(out) >= top:
            break
    return out


def evidence_pack(question: str, sources: int = 3, deadline: float = 9.0) -> dict | None:
    """Search, read the best few pages in parallel, return relevant passages."""
    t0 = time.monotonic()
    rows, engine = W._cascade("text", question, 10)
    if not rows:
        return None
    cands, seen_dom, seen_url = [], Counter(), set()
    for r in rows:
        url = r.get("href") or ""
        if not url.startswith("http"):
            continue
        d = _domain(url)
        if any(d == s or d.endswith("." + s) for s in _SKIP_DOMAINS):
            continue
        k = W._norm_url(url)
        if k in seen_url or seen_dom[d] >= 1:
            continue
        seen_url.add(k)
        seen_dom[d] += 1
        cands.append((_prior(url), r))
    # stable: engine order, nudged by how trustworthy the domain tends to be
    cands = sorted(enumerate(cands), key=lambda ic: (-(ic[1][0] - 0.04 * ic[0])))
    picked = [c for _, (_, c) in cands][:sources + 1]

    pages: list[dict] = []
    ex = cf.ThreadPoolExecutor(max_workers=len(picked) or 1)
    futs = {ex.submit(W._fetch_one, r["href"], 40000, (4, 7)): r for r in picked}
    try:
        for f in cf.as_completed(futs, timeout=max(1.0, deadline - (time.monotonic() - t0))):
            try:
                d = f.result()
            except Exception:
                continue
            if d.get("ok") and d.get("chars", 0) > 400:
                d["_snippet"] = W._clean(futs[f].get("body"))
                d["_rank"] = list(futs.values()).index(futs[f])
                pages.append(d)
            if len(pages) >= sources:
                break
    except cf.TimeoutError:
        pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    if not pages:
        # Nothing readable: the engine's own snippets are still evidence.
        return {"engine": engine, "evidence": [
            {"source": W._deglue(W._clean(r.get("title"))), "url": r.get("href"),
             "passages": [W._clean(r.get("body"))[:500]]} for r in picked[:5]
            if r.get("body")], "mode": "snippets-only",
            "seconds": round(time.monotonic() - t0, 1)}

    pages.sort(key=lambda d: d["_rank"])
    docs: list[tuple[int, str]] = []
    for i, d in enumerate(pages):
        for p in passages(d["content"]):
            docs.append((i, p))
        if d.get("_snippet"):
            docs.append((i, d["_snippet"]))
    best = rank_passages(question, docs, top=8, per_doc=3)
    evid: list[dict] = []
    for i, d in enumerate(pages):
        mine = [t for di, t, _ in best if di == i]
        if not mine:
            continue
        evid.append({"source": d.get("title") or _domain(d["url"]), "url": d["url"],
                     "date": d.get("date") or "", "passages": mine})
    return {"engine": engine, "evidence": evid, "mode": "evidence",
            "pages_read": len(pages), "seconds": round(time.monotonic() - t0, 1)}


# ---------------------------------------------------------------- the tool

@REGISTRY.tool(
    "web_answer",
    "The fastest way to answer any question that needs the internet: facts, "
    "current events, prices, versions, how-tos, 'what is X', error messages. "
    "One call searches, reads and answers with sources (usually 5-20 seconds). Use "
    "this FIRST instead of web_search + fetch_page. Use depth='thorough' for "
    "contested, technical or multi-part questions. For live numbers prefer "
    "stock_quote, crypto_price, currency_convert and get_weather, which are exact.",
    {"type": "object", "properties": {
        "question": {"type": "string",
                     "description": "The question, phrased fully - include names, "
                                    "versions, dates and what form the answer should take"},
        "depth": {"type": "string", "enum": ["quick", "thorough"],
                  "description": "quick (default, ~4 s) or thorough (~8 s, reads more)"}},
     "required": ["question"]},
    risk=Risk.SAFE, category="search", parallel_safe=True, slow=True)
def web_answer(question: str, depth: str = "quick"):
    question = (question or "").strip()
    if not question:
        return ToolResult(False, error="ask a question")
    depth = "thorough" if str(depth).lower().startswith("t") else "quick"
    key = _key(question, depth)
    hit = _ANSWERS.get(key)
    if hit is not None:
        return ToolResult(True, content={**hit, "cached": True},
                          display=f"answered from the web (cached): {question[:50]}")
    t0 = time.monotonic()
    g = _grounded(question, depth)
    if g:
        content = {"question": question, "answer": g["answer"],
                   "sources": g["sources"], "via": "grounded web search",
                   "seconds": round(time.monotonic() - t0, 1),
                   "checked": time.strftime("%Y-%m-%d %H:%M")}
        _ANSWERS.put(key, content, W.ttl_for(question))
        return ToolResult(True, content=content,
                          display=f"answered from the web: {question[:50]} "
                                  f"({len(g['sources'])} sources, "
                                  f"{content['seconds']}s)")
    pack = evidence_pack(question, sources=4 if depth == "thorough" else 3)
    if not pack or not pack.get("evidence"):
        return ToolResult(False, error=(
            "Could not reach the web. The network may be down; try again, or "
            "open a specific page with fetch_page."))
    content = {"question": question, "via": "search + page reading",
               "instruction": ("No ready-made answer: answer from the passages "
                               "below, cite the url of each source you use, and "
                               "say if they disagree or do not cover it."),
               **pack, "checked": time.strftime("%Y-%m-%d %H:%M")}
    _ANSWERS.put(key, content, W.ttl_for(question))
    n = len(pack["evidence"])
    return ToolResult(True, content=content,
                      display=f"researched: {question[:50]} ({n} sources, "
                              f"{pack.get('seconds', 0)}s)")
