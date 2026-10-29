"""
Exact answers, instantly: the things a web search gets wrong or answers late.

A search snippet for "bitcoin price" is hours old and a model will happily
"convert" currencies from memory. These tools hit the primary source directly
(0.3-1 s each, no model call) and return the figure with its timestamp:

    wikipedia        encyclopedia summary of a topic
    currency_convert live exchange rates
    crypto_price     live coin prices and 24 h change
    stock_quote      share prices (Yahoo chart data)
    define_word      dictionary definitions
    time_in          the current time in any city
    convert_units    length, mass, volume, speed, area, time, data, temperature
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
import urllib.parse
from zoneinfo import ZoneInfo

from ..safety.policy import Risk
from . import web as W
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.quickfacts")

_UA = "jarvis-local/1.0 (personal-desktop-assistant; non-commercial; low-volume)"
_CACHE = W.TTLCache(128)


def _get_json(url: str, params: dict | None = None, headers: dict | None = None,
              timeout: float = 8.0, ttl: float = 0.0):
    """GET and parse JSON through the shared pooled session; one retry for a
    dropped connection. Raises on failure."""
    key = (url, tuple(sorted((params or {}).items())))
    if ttl:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
    h = {"User-Agent": _UA, "Accept": "application/json"}
    h.update(headers or {})
    last = None
    for _ in range(2):
        try:
            r = W._session().get(url, params=params, headers=h, timeout=timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            if ttl:
                _CACHE.put(key, data, ttl)
            return data
        except Exception as e:                  # noqa: BLE001
            last = e
            time.sleep(0.15)
    raise last                                   # type: ignore[misc]


# ---------------------------------------------------------------- wikipedia

@REGISTRY.tool(
    "wikipedia",
    "Encyclopedia summary of a person, place, concept, event or product, with "
    "its link. Fast and reliable for 'what is / who is X'. If the topic is "
    "ambiguous the result lists the candidates.",
    {"type": "object", "properties": {
        "topic": {"type": "string"},
        "language": {"type": "string", "description": "Wikipedia language code, default en"}},
     "required": ["topic"]},
    risk=Risk.SAFE, category="search", parallel_safe=True, slow=False)
def wikipedia(topic: str, language: str = "en"):
    topic = (topic or "").strip()
    if not topic:
        return ToolResult(False, error="which topic?")
    lang = re.sub(r"[^a-z\-]", "", (language or "en").lower()) or "en"
    base = f"https://{lang}.wikipedia.org"

    def summary(title: str):
        return _get_json(f"{base}/api/rest_v1/page/summary/"
                         + urllib.parse.quote(title.replace(" ", "_"), safe=""),
                         ttl=3600)
    def extract_via_action_api(title: str):
        """Same text through the MediaWiki action API - a separate path that
        keeps working when the REST summary endpoint is rate-limiting."""
        j = _get_json(f"{base}/w/api.php", {
            "action": "query", "prop": "extracts|info", "exintro": 1, "explaintext": 1,
            "redirects": 1, "inprop": "url", "titles": title, "format": "json",
            "formatversion": 2}, ttl=3600)
        pages = ((j or {}).get("query") or {}).get("pages") or []
        if not pages or pages[0].get("missing"):
            return None
        p = pages[0]
        return {"title": p.get("title"), "extract": p.get("extract", ""),
                "description": "", "content_urls": {"desktop": {"page": p.get("fullurl", "")}}}

    try:
        try:
            d = summary(topic)
        except Exception as first:
            if "429" not in str(first) and "Too many" not in str(first):
                raise
            time.sleep(0.6)
            d = extract_via_action_api(topic)
            if d is None:
                raise first
        if d is None or d.get("type") == "https://mediawiki.org/wiki/HyperSwitch/errors/not_found":
            hits = _get_json(f"{base}/w/api.php", {
                "action": "query", "list": "search", "srsearch": topic,
                "format": "json", "srlimit": 5}, ttl=3600)
            titles = [h["title"] for h in (hits or {}).get("query", {}).get("search", [])]
            if not titles:
                return ToolResult(False, error=f"no Wikipedia article found for {topic!r}")
            d = summary(titles[0])
            if d is None:
                return ToolResult(False, error=f"no Wikipedia article found for {topic!r}")
            d["_matched"] = titles[0]
    except Exception as e:
        # Wikimedia rate-limits shared networks; a grounded search answers the
        # same question without needing their API.
        try:
            from .research import web_answer
            r = web_answer(f"What is {topic}? Give a short encyclopedic summary.")
            if r.ok and r.content.get("answer"):
                return ToolResult(True, content={
                    "title": topic, "summary": r.content["answer"],
                    "sources": r.content.get("sources", []),
                    "note": "Wikipedia was unavailable; summarised from web search"},
                    display=f"summary: {topic}")
        except Exception:
            pass
        return ToolResult(False, error=f"Wikipedia is unreachable: {e}"[:200])

    out = {"title": d.get("title"), "description": d.get("description", ""),
           "summary": d.get("extract", ""),
           "url": (d.get("content_urls", {}).get("desktop", {}) or {}).get("page", "")}
    if d.get("type") == "disambiguation":
        out["disambiguation"] = True
        out["note"] = "this name has several meanings; pick one and ask again"
    if d.get("_matched") and d["_matched"].lower() != topic.lower():
        out["matched_as"] = d["_matched"]
    return ToolResult(True, content=out, display=f"Wikipedia: {out['title']}")


# ---------------------------------------------------------------- currency

_CUR_NAMES = {
    "dollar": "USD", "dollars": "USD", "usd": "USD", "us dollar": "USD", "us dollars": "USD",
    "buck": "USD", "bucks": "USD",
    "rupee": "INR", "rupees": "INR", "inr": "INR", "indian rupee": "INR", "indian rupees": "INR",
    "euro": "EUR", "euros": "EUR", "eur": "EUR",
    "pound": "GBP", "pounds": "GBP", "gbp": "GBP", "sterling": "GBP", "quid": "GBP",
    "yen": "JPY", "jpy": "JPY", "yuan": "CNY", "rmb": "CNY", "cny": "CNY", "renminbi": "CNY",
    "franc": "CHF", "francs": "CHF", "chf": "CHF",
    "aud": "AUD", "australian dollar": "AUD", "australian dollars": "AUD",
    "cad": "CAD", "canadian dollar": "CAD", "canadian dollars": "CAD",
    "won": "KRW", "krw": "KRW", "peso": "MXN", "pesos": "MXN", "mxn": "MXN",
    "real": "BRL", "reais": "BRL", "brl": "BRL", "ruble": "RUB", "rubles": "RUB",
    "dirham": "AED", "dirhams": "AED", "aed": "AED", "riyal": "SAR", "sar": "SAR",
    "baht": "THB", "thb": "THB", "ringgit": "MYR", "myr": "MYR", "sgd": "SGD",
    "singapore dollar": "SGD", "nzd": "NZD", "hkd": "HKD", "sek": "SEK", "nok": "NOK",
    "dkk": "DKK", "pln": "PLN", "zar": "ZAR", "rand": "ZAR", "try": "TRY", "lira": "TRY",
    "idr": "IDR", "rupiah": "IDR", "php": "PHP", "vnd": "VND", "pkr": "PKR", "bdt": "BDT",
    "lkr": "LKR", "npr": "NPR", "egp": "EGP", "ngn": "NGN", "kes": "KES",
}


def _code(c: str) -> str:
    k = re.sub(r"\s+", " ", (c or "").strip().lower())
    if k in _CUR_NAMES:
        return _CUR_NAMES[k]
    return re.sub(r"[^A-Za-z]", "", c or "").upper()[:3]


@REGISTRY.tool(
    "currency_convert",
    "Convert an amount between currencies at the live exchange rate. Accepts "
    "codes (USD, INR) or names (dollars, rupees). Use this for any money "
    "conversion - never convert from memory.",
    {"type": "object", "properties": {
        "amount": {"type": "number"},
        "from_currency": {"type": "string"},
        "to_currency": {"type": "string"}},
     "required": ["amount", "from_currency", "to_currency"]},
    risk=Risk.SAFE, category="search", parallel_safe=True)
def currency_convert(amount: float, from_currency: str, to_currency: str):
    try:
        amt = float(str(amount).replace(",", ""))
    except Exception:
        return ToolResult(False, error=f"{amount!r} is not a number")
    src, dst = _code(from_currency), _code(to_currency)
    if len(src) != 3 or len(dst) != 3:
        return ToolResult(False, error="give currencies as codes (USD, EUR) or names")
    if src == dst:
        return ToolResult(True, content={"amount": amt, "from": src, "to": dst,
                                         "rate": 1.0, "converted": amt})
    rate, asof, source = None, "", ""
    try:
        d = _get_json(f"https://open.er-api.com/v6/latest/{src}", ttl=300)
        if d and d.get("result") == "success" and dst in d.get("rates", {}):
            rate = float(d["rates"][dst])
            asof = d.get("time_last_update_utc", "")
            source = "open.er-api.com"
    except Exception as e:
        log.info("er-api failed: %s", e)
    if rate is None:
        try:
            d = _get_json("https://api.frankfurter.app/latest",
                          {"from": src, "to": dst}, ttl=300)
            if d and dst in d.get("rates", {}):
                rate = float(d["rates"][dst])
                asof = d.get("date", "")
                source = "frankfurter.app (ECB)"
        except Exception as e:
            log.info("frankfurter failed: %s", e)
    if rate is None:
        return ToolResult(False, error=f"no live rate available for {src} to {dst}")
    conv = round(amt * rate, 4 if rate < 1 else 2)
    return ToolResult(True, content={
        "amount": amt, "from": src, "to": dst, "rate": rate, "converted": conv,
        "as_of": asof, "source": source,
        "note": "mid-market reference rate; banks and cards add a margin"},
        display=f"{amt:g} {src} = {conv:,} {dst}")


# ---------------------------------------------------------------- crypto

_COINS = {"btc": "bitcoin", "bitcoin": "bitcoin", "eth": "ethereum", "ethereum": "ethereum",
          "sol": "solana", "solana": "solana", "doge": "dogecoin", "dogecoin": "dogecoin",
          "xrp": "ripple", "ripple": "ripple", "ada": "cardano", "cardano": "cardano",
          "bnb": "binancecoin", "usdt": "tether", "tether": "tether", "usdc": "usd-coin",
          "ltc": "litecoin", "litecoin": "litecoin", "dot": "polkadot", "polkadot": "polkadot",
          "matic": "matic-network", "avax": "avalanche-2", "link": "chainlink",
          "shib": "shiba-inu", "trx": "tron", "ton": "the-open-network"}


@REGISTRY.tool(
    "crypto_price",
    "Live cryptocurrency price with 24-hour change and market cap. Accepts a "
    "name or ticker (bitcoin, BTC, eth).",
    {"type": "object", "properties": {
        "coin": {"type": "string"},
        "currency": {"type": "string", "description": "Quote currency code, default usd"}},
     "required": ["coin"]},
    risk=Risk.SAFE, category="search", parallel_safe=True)
def crypto_price(coin: str, currency: str = "usd"):
    q = (coin or "").strip().lower()
    cur = _code(currency or "usd").lower() or "usd"
    cid = _COINS.get(q)
    try:
        if not cid:
            s = _get_json("https://api.coingecko.com/api/v3/search", {"query": q}, ttl=3600)
            coins = (s or {}).get("coins", [])
            if not coins:
                return ToolResult(False, error=f"no coin matched {coin!r}")
            cid = coins[0]["id"]
        d = _get_json("https://api.coingecko.com/api/v3/simple/price", {
            "ids": cid, "vs_currencies": cur, "include_24hr_change": "true",
            "include_market_cap": "true", "include_last_updated_at": "true"}, ttl=20)
    except Exception as e:
        return ToolResult(False, error=f"price service unreachable: {e}"[:200])
    row = (d or {}).get(cid)
    if not row or cur not in row:
        return ToolResult(False, error=f"no {cur.upper()} price for {coin!r}")
    ts = row.get("last_updated_at")
    out = {"coin": cid, "currency": cur.upper(), "price": row[cur],
           "change_24h_percent": round(row.get(f"{cur}_24h_change", 0) or 0, 2),
           "market_cap": row.get(f"{cur}_market_cap"),
           "as_of": dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
           "source": "coingecko.com"}
    return ToolResult(True, content=out,
                      display=f"{cid}: {row[cur]:,} {cur.upper()} "
                              f"({out['change_24h_percent']:+}% 24h)")


# ---------------------------------------------------------------- stocks

@REGISTRY.tool(
    "stock_quote",
    "Latest share price for a ticker (AAPL, TSLA, RELIANCE.NS) or company name, "
    "with change since the previous close, day range and 52-week range.",
    {"type": "object", "properties": {"symbol": {"type": "string"}},
     "required": ["symbol"]},
    risk=Risk.SAFE, category="search", parallel_safe=True)
def stock_quote(symbol: str):
    s = (symbol or "").strip()
    if not s:
        return ToolResult(False, error="which stock?")
    # A name rather than a ticker: ask Yahoo's own search which one is meant.
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-=^]{0,11}", s):
        try:
            r = _get_json("https://query2.finance.yahoo.com/v1/finance/search",
                          {"q": s, "quotesCount": 5, "newsCount": 0},
                          headers={"User-Agent": "Mozilla/5.0"}, ttl=3600)
            quotes = [q for q in (r or {}).get("quotes", [])
                      if q.get("quoteType") in ("EQUITY", "ETF", "INDEX", "MUTUALFUND")]
            if not quotes:
                return ToolResult(False, error=f"no listed company matched {symbol!r}")
            s = quotes[0]["symbol"]
        except Exception as e:
            return ToolResult(False, error=f"stock search failed: {e}"[:200])
    try:
        d = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}",
                      {"range": "1d", "interval": "1d"},
                      headers={"User-Agent": "Mozilla/5.0"}, ttl=20)
    except Exception as e:
        return ToolResult(False, error=f"quote service unreachable: {e}"[:200])
    res = ((d or {}).get("chart") or {}).get("result")
    if not res:
        return ToolResult(False, error=f"no quote for {s!r} - check the ticker")
    m = res[0].get("meta", {})
    price, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
    if price is None:
        return ToolResult(False, error=f"no price available for {s!r}")
    chg = (price - prev) if prev else None
    ts = m.get("regularMarketTime")
    out = {"symbol": m.get("symbol", s), "name": m.get("longName") or m.get("shortName") or "",
           "price": price, "currency": m.get("currency", ""), "exchange": m.get("fullExchangeName")
           or m.get("exchangeName", ""),
           "previous_close": prev,
           "change": round(chg, 2) if chg is not None else None,
           "change_percent": round(chg / prev * 100, 2) if chg is not None and prev else None,
           "day_high": m.get("regularMarketDayHigh"), "day_low": m.get("regularMarketDayLow"),
           "52_week_high": m.get("fiftyTwoWeekHigh"), "52_week_low": m.get("fiftyTwoWeekLow"),
           "volume": m.get("regularMarketVolume"),
           "as_of": dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
           "source": "Yahoo Finance"}
    return ToolResult(True, content=out,
                      display=f"{out['symbol']}: {price:,} {out['currency']}"
                              + (f" ({out['change_percent']:+}%)" if out["change_percent"] is not None else ""))


# ---------------------------------------------------------------- dictionary

@REGISTRY.tool(
    "define_word",
    "Dictionary definition of an English word: meanings, examples, "
    "pronunciation and synonyms.",
    {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]},
    risk=Risk.SAFE, category="search", parallel_safe=True)
def define_word(word: str):
    w = (word or "").strip().lower()
    if not w or " " in w and len(w.split()) > 3:
        return ToolResult(False, error="give a single word or short phrase")
    try:
        d = _get_json("https://api.dictionaryapi.dev/api/v2/entries/en/"
                      + urllib.parse.quote(w), ttl=86400)
    except Exception as e:
        return ToolResult(False, error=f"dictionary unreachable: {e}"[:200])
    if not d or not isinstance(d, list):
        return ToolResult(False, error=f"no dictionary entry for {word!r}")
    e = d[0]
    meanings = []
    for m in e.get("meanings", [])[:4]:
        defs = m.get("definitions", [])[:2]
        meanings.append({"part_of_speech": m.get("partOfSpeech"),
                         "definitions": [{"definition": x.get("definition"),
                                          "example": x.get("example")} for x in defs],
                         "synonyms": (m.get("synonyms") or [])[:6]})
    return ToolResult(True, content={"word": e.get("word", w), "phonetic": e.get("phonetic", ""),
                                     "meanings": meanings},
                      display=f"defined {w}")


# ---------------------------------------------------------------- time zones

@REGISTRY.tool(
    "time_in",
    "Current local time, date and UTC offset in any city or place, and how it "
    "compares with the time here. Use for 'what time is it in Tokyo'.",
    {"type": "object", "properties": {"place": {"type": "string"}}, "required": ["place"]},
    risk=Risk.SAFE, category="search", parallel_safe=True)
def time_in(place: str):
    p = (place or "").strip()
    if not p:
        return ToolResult(False, error="which place?")
    try:
        g = _get_json("https://geocoding-api.open-meteo.com/v1/search",
                      {"name": p, "count": 1, "language": "en"}, ttl=86400)
    except Exception as e:
        return ToolResult(False, error=f"lookup failed: {e}"[:200])
    res = (g or {}).get("results") or []
    if not res or not res[0].get("timezone"):
        return ToolResult(False, error=f"could not find a place called {place!r}")
    r = res[0]
    now = dt.datetime.now(ZoneInfo(r["timezone"]))
    here = dt.datetime.now().astimezone()
    off_h = (now.utcoffset() - here.utcoffset()).total_seconds() / 3600
    return ToolResult(True, content={
        "place": ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x),
        "timezone": r["timezone"], "local_time": now.strftime("%I:%M %p").lstrip("0"),
        "date": now.strftime("%A, %d %B %Y"),
        "utc_offset": now.strftime("%z"),
        "hours_ahead_of_here": round(off_h, 2),
        "iso": now.isoformat(timespec="minutes")},
        display=f"{r.get('name')}: {now.strftime('%I:%M %p').lstrip('0')}")


# ---------------------------------------------------------------- unit conversion

_UNITS: dict[str, tuple[str, float]] = {}
_CATEGORIES: dict[str, dict[str, float]] = {
    "length": {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254,
               "ft": 0.3048, "yd": 0.9144, "mi": 1609.344, "nmi": 1852.0},
    "mass": {"mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0, "oz": 0.028349523125,
             "lb": 0.45359237, "st": 6.35029318},
    "volume": {"ml": 0.001, "l": 1.0, "m3": 1000.0, "tsp": 0.00492892159375,
               "tbsp": 0.01478676478125, "floz": 0.0295735295625, "cup": 0.2365882365,
               "pt": 0.473176473, "qt": 0.946352946, "gal": 3.785411784},
    "speed": {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 0.5144444444, "ft/s": 0.3048},
    "area": {"cm2": 1e-4, "m2": 1.0, "km2": 1e6, "ha": 1e4, "acre": 4046.8564224,
             "ft2": 0.09290304, "in2": 0.00064516, "mi2": 2589988.110336},
    "time": {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0, "day": 86400.0,
             "week": 604800.0, "year": 31557600.0},
    "data": {"bit": 0.125, "b": 0.125, "B": 1.0, "KB": 1e3, "MB": 1e6, "GB": 1e9,
             "TB": 1e12, "KiB": 1024.0, "MiB": 1048576.0, "GiB": 2.0 ** 30,
             "TiB": 2.0 ** 40, "kbit": 125.0, "Mbit": 125000.0, "Gbit": 1.25e8},
}
_ALIASES = {
    "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
    "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "kilometer": "km", "kilometers": "km", "kilometre": "km", "kilometres": "km", "kms": "km",
    "inch": "in", "inches": "in", "\"": "in", "foot": "ft", "feet": "ft", "'": "ft",
    "yard": "yd", "yards": "yd", "mile": "mi", "miles": "mi", "nautical mile": "nmi",
    "nautical miles": "nmi",
    "milligram": "mg", "milligrams": "mg", "gram": "g", "grams": "g", "gm": "g",
    "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg", "kgs": "kg",
    "tonne": "t", "tonnes": "t", "metric ton": "t", "ounce": "oz", "ounces": "oz",
    "pound": "lb", "pounds": "lb", "lbs": "lb", "stone": "st", "stones": "st",
    "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml",
    "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "teaspoon": "tsp", "teaspoons": "tsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "fluid ounce": "floz", "fluid ounces": "floz", "fl oz": "floz",
    "cups": "cup", "pint": "pt", "pints": "pt", "quart": "qt", "quarts": "qt",
    "gallon": "gal", "gallons": "gal",
    "kph": "km/h", "kmh": "km/h", "kmph": "km/h", "knot": "kn", "knots": "kn",
    "meters per second": "m/s", "mps": "m/s", "fps": "ft/s",
    "sq m": "m2", "sqm": "m2", "square meter": "m2", "square meters": "m2",
    "sq ft": "ft2", "sqft": "ft2", "square feet": "ft2", "square foot": "ft2",
    "hectare": "ha", "hectares": "ha", "acres": "acre",
    "millisecond": "ms", "milliseconds": "ms", "second": "s", "seconds": "s", "sec": "s",
    "minute": "min", "minutes": "min", "mins": "min", "hour": "h", "hours": "h", "hr": "h",
    "hrs": "h", "days": "day", "d": "day", "weeks": "week", "wk": "week",
    "years": "year", "yr": "year", "yrs": "year",
    "byte": "B", "bytes": "B", "kilobyte": "KB", "kilobytes": "KB", "kb": "KB",
    "megabyte": "MB", "megabytes": "MB", "mb": "MB", "gigabyte": "GB", "gigabytes": "GB",
    "gb": "GB", "terabyte": "TB", "terabytes": "TB", "tb": "TB",
    "kibibyte": "KiB", "mebibyte": "MiB", "gibibyte": "GiB", "bits": "bit",
}
_TEMPS = {"c": "c", "celsius": "c", "centigrade": "c", "°c": "c", "degc": "c",
          "f": "f", "fahrenheit": "f", "°f": "f", "degf": "f",
          "k": "k", "kelvin": "k"}


def _canon(u: str) -> tuple[str, str] | None:
    """(category, canonical unit) for a unit as written, or None."""
    raw = (u or "").strip()
    key = raw.lower().replace(".", "")
    if key in _TEMPS:
        return "temperature", _TEMPS[key]
    for name in (raw, _ALIASES.get(key), _ALIASES.get(raw), key):
        if not name:
            continue
        for cat, units in _CATEGORIES.items():
            if name in units:
                return cat, name
    # "square kilometres" etc: allow plain "sq <unit>" quickly
    return None


def _to_c(v: float, u: str) -> float:
    return v if u == "c" else (v - 32) * 5 / 9 if u == "f" else v - 273.15


def _from_c(c: float, u: str) -> float:
    return c if u == "c" else c * 9 / 5 + 32 if u == "f" else c + 273.15


@REGISTRY.tool(
    "convert_units",
    "Exact unit conversion: length, mass, volume (incl. cups, tbsp), speed, "
    "area, time, data sizes and temperature (C/F/K). Use instead of doing the "
    "arithmetic yourself.",
    {"type": "object", "properties": {
        "value": {"type": "number"},
        "from_unit": {"type": "string"},
        "to_unit": {"type": "string"}},
     "required": ["value", "from_unit", "to_unit"]},
    risk=Risk.SAFE, category="read", parallel_safe=True)
def convert_units(value: float, from_unit: str, to_unit: str):
    try:
        v = float(str(value).replace(",", ""))
    except Exception:
        return ToolResult(False, error=f"{value!r} is not a number")
    a, b = _canon(from_unit), _canon(to_unit)
    if not a:
        return ToolResult(False, error=f"unknown unit {from_unit!r}")
    if not b:
        return ToolResult(False, error=f"unknown unit {to_unit!r}")
    if a[0] != b[0]:
        return ToolResult(False, error=f"cannot convert {a[0]} ({a[1]}) to {b[0]} ({b[1]})")
    if a[0] == "temperature":
        out = _from_c(_to_c(v, a[1]), b[1])
    else:
        table = _CATEGORIES[a[0]]
        out = v * table[a[1]] / table[b[1]]
    shown = float(f"{out:.6g}")
    return ToolResult(True, content={"value": v, "from": a[1], "to": b[1],
                                     "category": a[0], "result": shown},
                      display=f"{v:g} {a[1]} = {shown:g} {b[1]}")
