"""
Fast, accurate web answers and exact-fact tools. Offline: every network call is
stubbed. Live checks run only when JARVIS_NO_LIVE is unset.
"""
import json
import os
import sys
import time
import types
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.tools import REGISTRY
from jarvis.tools import quickfacts as Q
from jarvis.tools import research as R
from jarvis.tools import web as W

RES = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    RES.append(bool(c))


print("=== registration ===")
for name in ("web_answer", "wikipedia", "currency_convert", "crypto_price",
             "stock_quote", "define_word", "time_in", "convert_units"):
    check(f"{name} is registered", REGISTRY.get(name) is not None)
check("web_answer is first-class: described as the first choice",
      "FIRST" in REGISTRY.get("web_answer").description)

print("\n=== parsing a grounded response ===")
PAYLOAD = {"output": [
    {"type": "reasoning"}, {"type": "web_search_call"},
    {"type": "message", "content": [{
        "type": "output_text",
        "text": "Canberra is the capital. ([wikipedia.org](https://en.wikipedia.org/wiki/Canberra))",
        "annotations": [
            {"type": "url_citation", "url": "https://en.wikipedia.org/wiki/Canberra?utm_source=x", "title": "Canberra"},
            {"type": "url_citation", "url": "https://en.wikipedia.org/wiki/Canberra", "title": "Canberra again"},
            {"type": "url_citation", "url": "https://www.britannica.com/place/Canberra", "title": "Britannica"},
        ]}]}]}
text, sources = R._parse_response(PAYLOAD)
check("inline citation markup is stripped", text == "Canberra is the capital.", text)
check("duplicate sources collapse (tracking params ignored)", len(sources) == 2, sources)
check("titles and urls are kept", sources[1]["url"].startswith("https://www.britannica"))

print("\n=== web_answer: grounded, cached, fallback ===")
calls = {"g": 0, "e": 0}


def fake_grounded(q, depth):
    calls["g"] += 1
    return {"answer": "Canberra.", "sources": [{"title": "Wiki", "url": "https://en.wikipedia.org/wiki/Canberra"}]}


real = (R._grounded, R.evidence_pack)
R._grounded = fake_grounded
R._ANSWERS.clear()
r = REGISTRY.execute("web_answer", {"question": "capital of Australia?"})
check("returns the grounded answer with sources",
      r.ok and r.content["answer"] == "Canberra." and r.content["sources"], r)
r2 = REGISTRY.execute("web_answer", {"question": "Capital of Australia"})
check("an equivalent question is served from cache", calls["g"] == 1 and r2.content.get("cached"), calls)

R._grounded = lambda q, d: None
R._ANSWERS.clear()
R.evidence_pack = lambda q, sources=3, deadline=9.0: {
    "engine": "stub", "mode": "evidence", "seconds": 1.0,
    "evidence": [{"source": "Doc", "url": "https://docs.example.com/x", "passages": ["use flexbox"]}]}
r = REGISTRY.execute("web_answer", {"question": "how do I centre a div"})
check("falls back to an evidence pack when grounding is unavailable",
      r.ok and r.content["via"] == "search + page reading" and r.content["evidence"], r)
check("and tells the model to answer only from the passages",
      "passages" in r.content["instruction"])
R.evidence_pack = lambda *a, **k: None
R._ANSWERS.clear()
r = REGISTRY.execute("web_answer", {"question": "something unanswerable"})
check("no web at all is an honest error", not r.ok and "web" in r.error.lower(), r)
R._grounded, R.evidence_pack = real

print("\n=== grounded call: how failures are handled ===")


class FakeResp:
    def __init__(self, data): self.data = data
    def read(self): return json.dumps(self.data).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


import jarvis.core.azure as A
A._TOKEN.get = lambda: "tok"
real_az, real_settings = A._az, A.settings
A._az = lambda: "/usr/bin/az"
A.settings = lambda: {"endpoint": "https://x.openai.azure.com/", "deployment": "gpt-6-sol"}
sent = []
seq = []


def fake_urlopen(req, timeout=0):
    body = json.loads(req.data)
    sent.append(body)
    nxt = seq.pop(0)
    if isinstance(nxt, Exception):
        raise nxt
    return FakeResp(nxt)


from jarvis.core import http_pool as HP
real_open = HP.urlopen
HP.urlopen = fake_urlopen
R._UNSUPPORTED_UNTIL = 0.0
seq[:] = [PAYLOAD]
g = R._grounded("capital of australia", "quick")
b = sent[-1]
check("uses the hosted web_search tool, required, effort none",
      b["tools"][0]["type"] == "web_search" and b["tool_choice"] == "required"
      and b["reasoning"]["effort"] == "none", b)
check("returns the answer and sources", g and g["answer"].startswith("Canberra") and len(g["sources"]) == 2)
sent.clear()
seq[:] = [PAYLOAD]
R._grounded("compare x and y in depth", "thorough")
check("thorough reads more and reasons a little",
      sent[-1]["tools"][0]["search_context_size"] == "medium"
      and sent[-1]["reasoning"]["effort"] == "low", sent[-1])

err400 = urllib.error.HTTPError("u", 400, "bad", {}, None)
err400.read = lambda: b'{"error": "reasoning effort none is not supported"}'
sent.clear()
seq[:] = [err400, PAYLOAD]
g = R._grounded("q", "quick")
check("a model that wants some reasoning is retried with low effort",
      g and sent[-1]["reasoning"]["effort"] == "low", sent)

unsupported = urllib.error.HTTPError("u", 404, "nf", {}, None)
unsupported.read = lambda: b'{"error": "no such tool"}'
seq[:] = [unsupported]
check("a refusal returns None (so the fallback runs)", R._grounded("q", "quick") is None)
check("...and is remembered, so the next call does not even try",
      R._UNSUPPORTED_UNTIL > time.time() and R._grounded("q", "quick") is None)
R._UNSUPPORTED_UNTIL = 0.0
HP.urlopen = real_open
A._az, A.settings = real_az, real_settings

print("\n=== evidence ranking ===")
doc_a = ("The Eiffel Tower is in Paris. It was finished in 1889.\n\n"
         "Gustave Eiffel's company designed and built the tower. Visitors can climb the stairs.")
doc_b = ("Our hotel offers great rates on rooms in Paris near many sights.\n\n"
         "Book now and save on your Paris trip with our exclusive offers.")
ps = [(0, p) for p in R.passages(doc_a)] + [(1, p) for p in R.passages(doc_b)]
best = R.rank_passages("when was the Eiffel Tower finished", ps, top=2)
check("the passage that answers the question ranks first",
      best and "1889" in best[0][1], best)
check("a document is limited to its best passages", len([b for b in best if b[0] == 0]) <= 3)
check("source priors prefer reference sites",
      R._prior("https://en.wikipedia.org/wiki/X") > R._prior("https://www.quora.com/q"))
check("government and education domains are trusted", R._prior("https://nih.gov/a") >= 0.9)

print("\n=== search caching, de-duplication, tidy titles ===")
n = {"c": 0}
real_cascade = W._cascade


def fake_cascade(kind, q, k, region="us-en", deadline=20.0):
    n["c"] += 1
    return ([{"title": "TheNobelPrizeinPhysics2024-NobelPrize.org", "href": "https://www.nobelprize.org/a?utm_source=x", "body": "b"},
             {"title": "Same page", "href": "https://nobelprize.org/a", "body": "b"},
             {"title": "Other", "href": "https://example.com/z", "body": "c"}], "stub")


W._cascade = fake_cascade
W._SEARCHES.clear()
r = W.web_search("nobel physics 2024")
check("duplicate pages (tracking params, www) are collapsed", len(r.content["results"]) == 2, r.content)
check("glued titles get their spaces back", "Nobel" in r.content["results"][0]["title"]
      and " " in r.content["results"][0]["title"])
W.web_search("nobel physics 2024")
check("repeating a search is served from cache", n["c"] == 1, n)
check("time-sensitive queries are remembered only briefly",
      W.ttl_for("bitcoin price today") < W.ttl_for("capital of australia"))
W._cascade = real_cascade

print("\n=== exact-fact tools (stubbed network) ===")


def stub_json(table):
    def f(url, params=None, headers=None, timeout=8.0, ttl=0.0):
        for k, v in table.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return v
        raise RuntimeError("unexpected " + url)
    return f


real_get = Q._get_json
Q._CACHE.clear()
Q._get_json = stub_json({"open.er-api.com": {"result": "success", "time_last_update_utc": "now",
                                             "rates": {"INR": 90.0}}})
r = Q.currency_convert(10, "dollars", "rupees")
check("currency: names resolve and the amount converts",
      r.ok and r.content["from"] == "USD" and r.content["to"] == "INR" and r.content["converted"] == 900.0, r)
Q._get_json = stub_json({"open.er-api.com": RuntimeError("down"),
                         "frankfurter": {"date": "2026-10-12", "rates": {"EUR": 0.5}}})
r = Q.currency_convert(4, "USD", "EUR")
check("currency: falls back to a second source", r.ok and r.content["converted"] == 2.0, r)
check("currency: junk amounts are refused", not Q.currency_convert("abc", "USD", "EUR").ok)

Q._get_json = stub_json({"coingecko.com/api/v3/simple/price": {"bitcoin": {
    "usd": 100000, "usd_24h_change": -1.234, "usd_market_cap": 5}}})
r = Q.crypto_price("BTC")
check("crypto: ticker maps to the coin", r.ok and r.content["coin"] == "bitcoin"
      and r.content["change_24h_percent"] == -1.23, r)

Q._get_json = stub_json({"finance/chart": {"chart": {"result": [{"meta": {
    "symbol": "AAPL", "longName": "Apple Inc.", "regularMarketPrice": 110.0,
    "chartPreviousClose": 100.0, "currency": "USD", "regularMarketTime": 1700000000}}]}}})
r = Q.stock_quote("AAPL")
check("stock: change and percent are computed",
      r.ok and r.content["change"] == 10.0 and r.content["change_percent"] == 10.0, r)

Q._get_json = stub_json({"dictionaryapi": [{"word": "ephemeral", "phonetic": "/x/", "meanings": [
    {"partOfSpeech": "adj", "definitions": [{"definition": "short-lived"}], "synonyms": ["brief"]}]}]})
r = Q.define_word("ephemeral")
check("dictionary: definition returned", r.ok and r.content["meanings"][0]["definitions"][0]["definition"] == "short-lived")

Q._get_json = stub_json({"geocoding-api": {"results": [{"name": "Tokyo", "country": "Japan",
                                                       "timezone": "Asia/Tokyo"}]}})
r = Q.time_in("Tokyo")
check("time zone: local time and offset are returned",
      r.ok and r.content["timezone"] == "Asia/Tokyo" and r.content["utc_offset"] == "+0900", r)
Q._get_json = stub_json({"geocoding-api": {"results": []}})
check("time zone: unknown place is an honest error", not Q.time_in("Zzyzx Nowhere").ok)

Q._get_json = stub_json({"/page/summary/": {"title": "Canberra", "extract": "Capital of Australia.",
                                            "description": "city",
                                            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Canberra"}}}})
r = Q.wikipedia("Canberra")
check("wikipedia: summary and link", r.ok and r.content["summary"].startswith("Capital"), r)
Q._get_json = real_get

print("\n=== unit conversion is exact ===")
cases = [(5, "miles", "km", 8.04672), (100, "fahrenheit", "celsius", 37.7778),
         (0, "celsius", "kelvin", 273.15), (2, "gallons", "litres", 7.57082),
         (1, "GB", "MB", 1000.0), (1, "GiB", "MiB", 1024.0), (60, "mph", "km/h", 96.5606),
         (1.5, "hours", "minutes", 90.0), (10, "stone", "kg", 63.5029)]
for v, a, b, want in cases:
    r = Q.convert_units(v, a, b)
    check(f"{v} {a} -> {b} = {want}", r.ok and abs(r.content["result"] - want) < 1e-3 * max(1, abs(want)), r)
check("mixing categories is refused", not Q.convert_units(1, "kg", "km").ok)
check("unknown units are refused", not Q.convert_units(1, "parsecs-per-fortnight", "km").ok)

if not os.environ.get("JARVIS_NO_LIVE"):
    print("\n=== live ===")
    t0 = time.time()
    r = REGISTRY.execute("web_answer", {"question": "What is the capital of Australia?"})
    dt = time.time() - t0
    txt = json.dumps(r.content, ensure_ascii=False) if r.ok else r.error
    check("live: web_answer says Canberra", r.ok and "Canberra" in txt, txt[:200])
    check(f"live: and quickly ({dt:.1f}s)", dt < 12)
    r = REGISTRY.execute("convert_units", {"value": 1, "from_unit": "mile", "to_unit": "km"})
    check("live: conversion", r.ok and abs(r.content["result"] - 1.60934) < 1e-3)

print("\n=== citation markers never reach the reply ===")
from jarvis.tools import research as _R
check("private-use cite markers are removed",
      _R._clean_answer("John Ternus is CEO.\ue200cite\ue202turn0search0\ue202turn0news1\ue201 Tim Cook is chair.")
      == "John Ternus is CEO. Tim Cook is chair.", _R._clean_answer("A.\ue200cite\ue202turn0search0\ue201"))
check("a bare 'citeturn0search0' is removed too", _R._clean_answer("Yes citeturn0search0") == "Yes", _R._clean_answer("Yes citeturn0search0"))
check("ordinary text mentioning 'cite' is untouched", _R._clean_answer("Please cite your sources.") == "Please cite your sources.")

print(f"\n{sum(RES)}/{len(RES)} research checks passed")
sys.exit(0 if all(RES) else 1)
