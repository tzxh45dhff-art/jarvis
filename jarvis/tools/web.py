"""
Web research: search, news, academic lookup, and real page extraction.

Sources are always returned with their URLs so the model can cite accurately.
Nothing here invents a citation - a source only exists if it was fetched.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import re
import threading
import time
import urllib.parse

from ..safety.policy import Risk
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.web")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_PAGE = 60_000


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


class TTLCache:
    """Small thread-safe cache with per-entry expiry. Repeating a search or a
    page read within a few minutes should cost nothing."""

    def __init__(self, cap: int = 128):
        self.cap = cap
        self._d: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if not hit:
                return None
            exp, val = hit
            if time.time() > exp:
                self._d.pop(key, None)
                return None
            return val

    def put(self, key, val, ttl: float):
        with self._lock:
            if len(self._d) >= self.cap:
                oldest = min(self._d, key=lambda k: self._d[k][0])
                self._d.pop(oldest, None)
            self._d[key] = (time.time() + ttl, val)

    def clear(self):
        with self._lock:
            self._d.clear()


_SEARCHES = TTLCache(128)
_PAGES = TTLCache(48)
_SESSION = None
_SESSION_LOCK = threading.Lock()
MAX_BYTES = 2_500_000          # enough for any article; stops a huge download
_TIME_SENSITIVE = re.compile(
    r"\b(today|now|current(ly)?|latest|live|price|score|weather|news|breaking|"
    r"tonight|tomorrow|yesterday|this (week|month)|stock|rate|trending)\b", re.I)


def ttl_for(query: str) -> float:
    """Things that change by the minute are remembered briefly."""
    return 45.0 if _TIME_SENSITIVE.search(query or "") else 600.0


def _session():
    """One pooled HTTP session: keep-alive connections make the second and
    later requests to a host skip the TCP and TLS handshake."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            import requests
            from requests.adapters import HTTPAdapter
            sess = requests.Session()
            sess.headers.update({"User-Agent": _UA,
                                 "Accept-Language": "en-US,en;q=0.9"})
            ad = HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
            sess.mount("https://", ad)
            sess.mount("http://", ad)
            _SESSION = sess
        return _SESSION


_GLUED = re.compile(r"(?<=[a-z]{3})(?=[A-Z])|(?<=\d)(?=[A-Za-z])|(?<=[a-z])(?=\d)")


def _deglue(title: str) -> str:
    """Some engines return titles with the spaces stripped
    ('TheNobelPrizeinPhysics2024'). Put back what can be put back safely."""
    t = title or ""
    if len(t) < 18 or t.count(" ") / len(t) > 0.07:
        return t
    return re.sub(r"\s+", " ", _GLUED.sub(" ", t)).strip()


def _norm_url(u: str) -> str:
    """Same page, different tracking junk -> same key."""
    try:
        p = urllib.parse.urlsplit(u or "")
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
             if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "spm"))]
        host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
        return urllib.parse.urlunsplit(("https", host, p.path.rstrip("/"),
                                        urllib.parse.urlencode(q), ""))
    except Exception:
        return u or ""


# Search engines fail independently and often: measured on this machine,
# duckduckgo, google, mojeek and yahoo all returned "no results" while brave and
# yandex answered normally, and which ones work changes with the network.
#
# So queries cascade through engines rather than trusting one. They run
# sequentially, not in parallel: ddgs is backed by a Rust HTTP client that holds
# the GIL for the duration of a request, so racing five backends in threads
# starves the interpreter completely - the main thread stops being scheduled at
# all. Measured, a working engine answers in about 2.5 s, and the engine that
# last worked is remembered and tried first, so the steady state is one call.
_TEXT_BACKENDS = ("brave", "yandex", "duckduckgo", "google", "mojeek", "yahoo")
_NEWS_BACKENDS = ("brave", "yandex", "duckduckgo", "yahoo")
SEARCH_DEADLINE = 20.0
_LAST_GOOD: dict[str, str] = {}


def _ddgs():
    from ddgs import DDGS
    return DDGS()


# One search at a time, process-wide. ddgs's Rust HTTP client holds the GIL
# for a whole request; two overlapping searches (the model batching web_search
# calls, which run in parallel threads) froze the entire engine - every thread
# starved, the turn never finished, and only a restart recovered it.
_DDGS_LOCK = threading.Lock()


def _one_backend(kind: str, query: str, n: int, backend: str,
                 region: str = "us-en") -> list[dict]:
    from ddgs import DDGS
    if not _DDGS_LOCK.acquire(timeout=30):
        raise TimeoutError("another web search is still running")
    try:
        with DDGS(timeout=8) as d:
            fn = d.news if kind == "news" else d.text
            kw = {"max_results": n, "backend": backend}
            if kind != "news":
                kw["region"] = region
            return list(fn(query, **kw))
    finally:
        _DDGS_LOCK.release()


def _cascade(kind: str, query: str, n: int, region: str = "us-en",
             deadline: float = SEARCH_DEADLINE) -> tuple[list[dict], str]:
    """Try engines in turn until one answers. Returns (rows, engine)."""
    order = list(_NEWS_BACKENDS if kind == "news" else _TEXT_BACKENDS)
    last = _LAST_GOOD.get(kind)
    if last in order:                       # prefer whatever worked last time
        order.remove(last)
        order.insert(0, last)
    started = time.monotonic()
    errors: list[str] = []
    for b in order:
        if time.monotonic() - started > deadline:
            break
        try:
            rows = _one_backend(kind, query, n, b, region)
        except Exception as e:              # noqa: BLE001
            errors.append(f"{b}:{type(e).__name__}")
            continue
        if rows:
            _LAST_GOOD[kind] = b
            return rows, b
        errors.append(f"{b}:empty")
    if errors:
        log.warning("search cascade exhausted for %r: %s", query[:60],
                    " ".join(errors))
    return [], ""


@REGISTRY.tool(
    "web_search",
    "Search the web and get titles, URLs and snippets. For a direct answer to "
    "a factual or current-events question use web_answer instead (one call, "
    "cited). Use this when you need the list of sources themselves, or to find "
    "pages to read with fetch_page.",
    {"type": "object", "properties": {
        "query": {"type": "string"},
        "max_results": {"type": "integer", "description": "Default 6"},
        "region": {"type": "string"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="search", slow=True)
def web_search(query: str, max_results: int = 6, region: str = "us-en"):
    n = max(1, min(max_results, 15))
    key = ("text", _clean(query).lower(), region, n)
    hit = _SEARCHES.get(key)
    if hit is not None:
        return ToolResult(True, content={**hit, "engine": hit.get("engine", "") + " (cached)"},
                          display=f"searched: {query[:56]} ({len(hit['results'])} results, cached)")
    rows, backend = _cascade("text", query, n, region)
    if not rows:
        return ToolResult(False, error=(
            "No search engine responded. The network may be down; try again, "
            "or fetch a specific URL directly."))
    seen: set[str] = set()
    out = []
    for r in rows:
        href = r.get("href")
        if not href:
            continue
        k = _norm_url(href)
        if k in seen:
            continue
        seen.add(k)
        out.append({"title": _deglue(_clean(r.get("title"))), "url": href,
                    "snippet": _clean(r.get("body"))[:400]})
    content = {"query": query, "results": out, "engine": backend}
    _SEARCHES.put(key, content, ttl_for(query))
    return ToolResult(True, content=content,
                      display=f"searched: {query[:56]} ({len(out)} results)")


@REGISTRY.tool(
    "news_search",
    "Search recent news articles with dates and sources. Use for 'what happened today' "
    "or 'latest' questions.",
    {"type": "object", "properties": {
        "query": {"type": "string"}, "max_results": {"type": "integer"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="search", slow=True)
def news_search(query: str, max_results: int = 8):
    rows, backend = _cascade("news", query, max(1, min(max_results, 20)))
    if not rows:
        return ToolResult(False, error="No news engine responded; try web_search.")
    out = [{"title": _clean(r.get("title")), "url": r.get("url") or r.get("href"),
            "source": r.get("source"), "date": r.get("date"),
            "snippet": _clean(r.get("body"))[:300]} for r in rows]
    return ToolResult(True, content={"query": query, "articles": out},
                      display=f"news: {query[:60]} ({len(out)} articles)")


def _decode(body: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8"]:
        try:
            return body.decode(enc)
        except Exception:
            continue
    # No usable charset: a <meta> tag usually says.
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", body[:4096], re.I)
    if m:
        try:
            return body.decode(m.group(1).decode(), errors="replace")
        except Exception:
            pass
    return body.decode("latin-1", errors="replace")


def _pdf_text(body: bytes, pages: int = 40) -> tuple[str, str]:
    import io
    from pypdf import PdfReader
    rd = PdfReader(io.BytesIO(body))
    meta = rd.metadata
    title = (getattr(meta, "title", "") or "") if meta else ""
    chunks = []
    for pg in rd.pages[:pages]:
        try:
            chunks.append(pg.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks), title


def _fetch_one(url: str, max_chars: int = MAX_PAGE, timeout=(5, 12)) -> dict:
    """Download and extract the readable content of one page (or PDF)."""
    hit = _PAGES.get(url)
    if hit is not None:
        return {**hit, "content": hit["content"][:max_chars], "cached": True}
    try:
        r = _session().get(url, timeout=timeout, allow_redirects=True, stream=True)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        chunks, got = [], 0
        for part in r.iter_content(chunk_size=65536):
            chunks.append(part)
            got += len(part)
            if got >= MAX_BYTES * (4 if "pdf" in ctype.lower() else 1):
                break
        r.close()
        body = b"".join(chunks)
    except Exception as e:
        return {"url": url, "ok": False, "error": str(e)[:200]}

    text, title, date, author = "", "", "", ""
    if "pdf" in ctype.lower() or body[:5] == b"%PDF-":
        try:
            text, title = _pdf_text(body)
        except Exception as e:
            return {"url": url, "ok": False, "error": f"PDF could not be read: {e}"[:200]}
        out = {"url": url, "ok": True, "title": _clean(title), "date": "", "author": "",
               "chars": len(text), "content": text[:MAX_PAGE * 4], "kind": "pdf"}
        _PAGES.put(url, out, 900)
        return {**out, "content": out["content"][:max_chars]}

    html = _decode(body, ctype)
    try:
        import trafilatura
        from trafilatura.settings import use_config
        cfg = use_config()
        cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "20")
        text = trafilatura.extract(html, include_comments=False,
                                   include_tables=True, favor_precision=True,
                                   config=cfg) or ""
        md = trafilatura.extract_metadata(html)
        if md:
            title = md.title or ""
            date = md.date or ""
            author = md.author or ""
    except Exception:
        pass
    if not text:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            for t in soup(["script", "style", "nav", "footer", "header", "aside"]):
                t.decompose()
            text = _clean(soup.get_text(" "))
            title = title or (soup.title.string if soup.title else "")
        except Exception as e:
            return {"url": url, "ok": False, "error": f"extraction failed: {e}"}
    out = {"url": url, "ok": True, "title": _clean(title), "date": date,
           "author": _clean(author), "chars": len(text),
           "content": text[:MAX_PAGE * 4]}
    _PAGES.put(url, out, 900)
    return {**out, "content": out["content"][:max_chars]}


PAGE_CHUNK = 9000      # fits the agent's per-result budget with room to spare


@REGISTRY.tool(
    "fetch_page",
    "Download a web page and extract its readable text, title, author and date. "
    "Use to actually read a source rather than relying on a search snippet. "
    "Long pages come in parts: if the result says more remains, call again with "
    "the given offset. Pages built by JavaScript come back nearly empty - then "
    "use browser_open and browser_read instead.",
    {"type": "object", "properties": {
        "url": {"type": "string"},
        "offset": {"type": "integer", "description": "Character to start from, for later parts"},
        "max_chars": {"type": "integer", "description": "Default 9000"}},
     "required": ["url"]},
    risk=Risk.SAFE, category="search", slow=True)
def fetch_page(url: str, offset: int = 0, max_chars: int = PAGE_CHUNK):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    d = _fetch_one(url, MAX_PAGE * 4)
    if not d.get("ok"):
        return ToolResult(False, error=d.get("error", "fetch failed"))
    full = d.get("content", "")
    size = max(500, min(int(max_chars or PAGE_CHUNK), 20000))
    start = max(0, int(offset or 0))
    part = full[start:start + size]
    d["content"] = part
    d["total_chars"] = len(full)
    d["offset"] = start
    if start + size < len(full):
        d["more"] = True
        d["next_offset"] = start + size
        d["note"] = (f"showing characters {start:,}-{start + len(part):,} of "
                     f"{len(full):,}; call fetch_page again with offset="
                     f"{start + size} for the rest")
    if len(full) < 400:
        d["warning"] = ("almost no text was extracted - this page is probably "
                        "built by JavaScript or needs a login. Use browser_open "
                        "then browser_read to see what a person would see.")
    return ToolResult(True, content=d,
                      display=f"read {d.get('title') or url[:50]} "
                              f"({len(part):,}/{len(full):,} chars)")


@REGISTRY.tool(
    "fetch_many",
    "Download and extract several web pages in parallel. Much faster than "
    "calling fetch_page repeatedly during research.",
    {"type": "object", "properties": {
        "urls": {"type": "array", "items": {"type": "string"}},
        "max_chars": {"type": "integer", "description": "Per page, default 20000"}},
     "required": ["urls"]},
    risk=Risk.SAFE, category="search", slow=True)
def fetch_many(urls: list, max_chars: int = 20000):
    if isinstance(urls, str):
        urls = [urls]
    seen, uniq = set(), []
    for u in urls:
        u = u if u.startswith("http") else "https://" + u
        if _norm_url(u) not in seen:
            seen.add(_norm_url(u))
            uniq.append(u)
    urls = uniq[:10]
    with cf.ThreadPoolExecutor(max_workers=min(8, len(urls) or 1)) as ex:
        pages = list(ex.map(lambda u: _fetch_one(u, max_chars), urls))
    ok = [p for p in pages if p.get("ok")]
    return ToolResult(True, content={"pages": pages, "fetched": len(ok),
                                     "failed": len(pages) - len(ok)},
                      display=f"fetched {len(ok)}/{len(pages)} pages")


_ARXIV_FIELD = re.compile(r"\b(ti|au|abs|co|jr|cat|rn|id|all):", re.I)


def _arxiv_query(query: str) -> str:
    """arXiv search_query for free text or for a query already written in
    arXiv's own syntax. Prefixing `all:` to 'ti:"X" AND au:"Y"' made
    'all:ti:...', which arXiv rejects with 400 (4 of 7 real calls)."""
    q = " ".join((query or "").split())
    if _ARXIV_FIELD.search(q):
        return re.sub(r"\b(and|or|andnot)\b", lambda m: m.group(1).upper(), q)
    return f"all:{q}"


def _openalex(query: str, n: int) -> list[dict]:
    """Fallback for when arXiv is slow, rate-limited (429) or simply does not
    have the paper (most journals and conferences are not on arXiv)."""
    import requests
    text = _ARXIV_FIELD.sub(" ", query)
    text = re.sub(r"\b(AND|OR|ANDNOT)\b|[\"()]", " ", text)
    text = " ".join(text.split())
    r = requests.get("https://api.openalex.org/works",
                     params={"search": text, "per-page": n},
                     headers={"User-Agent": _UA}, timeout=12)
    r.raise_for_status()
    out = []
    for w in r.json().get("results") or []:
        inv = w.get("abstract_inverted_index") or {}
        pos = sorted((i, word) for word, idx in inv.items() for i in idx)
        loc = (w.get("primary_location") or {}).get("landing_page_url") or ""
        out.append({
            "title": _clean(w.get("title") or w.get("display_name") or ""),
            "authors": [_clean(((a.get("author") or {}).get("display_name")) or "")
                        for a in (w.get("authorships") or [])][:8],
            "published": (w.get("publication_date") or str(w.get("publication_year") or ""))[:10],
            "summary": _clean(" ".join(word for _, word in pos))[:900],
            "url": w.get("doi") or loc or w.get("id") or "",
            "cited_by": w.get("cited_by_count"),
        })
    return out


@REGISTRY.tool(
    "search_papers",
    "Search academic papers (title, authors, abstract, date, link): arXiv "
    "first, then OpenAlex, which covers journals and conferences too. Plain "
    "words or arXiv syntax like ti:\"...\" AND au:\"...\". Use for scientific "
    "literature reviews.",
    {"type": "object", "properties": {
        "query": {"type": "string"}, "max_results": {"type": "integer"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="search", slow=True)
def search_papers(query: str, max_results: int = 8):
    import requests
    from xml.etree import ElementTree as ET
    n = max(1, min(int(max_results or 8), 25))
    q = urllib.parse.quote(_arxiv_query(query), safe=":")
    url = (f"https://export.arxiv.org/api/query?search_query={q}"
           f"&start=0&max_results={n}"
           f"&sortBy=relevance&sortOrder=descending")
    papers, arxiv_err = [], ""
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns):
            def g(tag):
                n_ = e.find(f"a:{tag}", ns)
                return _clean(n_.text) if n_ is not None and n_.text else ""
            if not g("title") or g("title").lower() == "error":
                continue
            papers.append({
                "title": g("title"),
                "authors": [_clean(a.findtext("a:name", "", ns))
                            for a in e.findall("a:author", ns)][:8],
                "published": g("published")[:10],
                "summary": g("summary")[:900],
                "url": g("id"),
            })
    except Exception as e:
        arxiv_err = str(e)[:160]
    if papers:
        return ToolResult(True, content={"query": query, "source": "arXiv",
                                         "papers": papers},
                          display=f"arXiv: {len(papers)} paper(s) for {query[:50]}")
    try:
        papers = _openalex(query, n)
    except Exception as e:
        return ToolResult(False, error=(
            f"paper search failed - arXiv: {arxiv_err or 'no results'}; "
            f"OpenAlex: {str(e)[:160]}"))
    if not papers:
        return ToolResult(False, error=f"no papers found for {query!r} on arXiv or "
                                       f"OpenAlex; try fewer or different words")
    content = {"query": query, "source": "OpenAlex", "papers": papers}
    if arxiv_err:
        content["note"] = f"arXiv did not answer ({arxiv_err[:80]}); results from OpenAlex"
    return ToolResult(True, content=content,
                      display=f"OpenAlex: {len(papers)} paper(s) for {query[:50]}")
