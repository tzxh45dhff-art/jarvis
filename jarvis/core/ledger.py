"""
What every model request cost, in rupees.

Azure's own cost view lags about a day and only shows totals per meter, so it
could not say *which* requests were expensive. Every Azure call now appends one
line to ~/.jarvis/data/usage.jsonl (model, kind of call, token counts, ₹), and
`jarvis cost` sums it.

Measured from Azure's cost view for 12-19 Oct 2026, the biggest line was not
output or plain input but **cache writes**: the prompt (13.8k tokens of
instructions and tools, plus the conversation) re-stored at 1.25x the input
price every time the cache had expired between requests.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict

from ..config import DATA_DIR

LEDGER = os.environ.get("JARVIS_USAGE_LEDGER") or str(DATA_DIR / "usage.jsonl")

# ₹ per 1M tokens: input, cached input, cache write, output (Azure retail,
# Global Standard, short context, Oct 2026). Astra's cache-write price is not
# on the price list; it follows the 1.25x-input rule of the others.
PRICES = {
    "gpt-6-sol": (191.98, 19.20, 239.97, 959.89),
    "gpt-6.1-sol": (191.98, 9.60, 239.97, 959.89),
    "gpt-6-luna": (9.60, 0.96, 12.00, 47.99),
    "gpt-6-astra": (959.89, 95.99, 1199.86, 4799.47),
}
SEARCH_INR = 0.48          # one hosted web search ("Search Transactions" meter)

_lock = threading.Lock()


def cost(model: str, prompt: int, cached: int = 0, write: int = 0, out: int = 0,
         searches: int = 0) -> float:
    p = PRICES.get(model, PRICES["gpt-6-sol"])
    plain = max(0, prompt - cached - write)
    return (plain * p[0] + cached * p[1] + write * p[2] + out * p[3]) / 1e6 \
        + searches * SEARCH_INR


def record(model: str, kind: str, prompt: int, cached: int = 0, write: int = 0,
           out: int = 0, searches: int = 0) -> float:
    """Append one request to the ledger. Never raises: a full disk must not
    break a reply."""
    inr = cost(model, prompt, cached, write, out, searches)
    row = {"t": round(time.time(), 1), "model": model, "kind": kind, "in": prompt,
           "cached": cached, "write": write, "out": out, "inr": round(inr, 4)}
    if searches:
        row["searches"] = searches
    try:
        with _lock, open(LEDGER, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return inr


def rows(since: float = 0.0) -> list[dict]:
    out = []
    try:
        with open(LEDGER) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= since:
                    out.append(r)
    except OSError:
        pass
    return out


def summary(days: float = 1.0) -> dict:
    """Totals for the last `days` days, by model and by kind of call."""
    rs = rows(time.time() - days * 86400)
    tot = {"requests": len(rs), "inr": 0.0, "in": 0, "cached": 0, "write": 0, "out": 0}
    by_model: dict = defaultdict(lambda: {"requests": 0, "inr": 0.0})
    by_kind: dict = defaultdict(lambda: {"requests": 0, "inr": 0.0})
    for r in rs:
        for k in ("in", "cached", "write", "out"):
            tot[k] += r.get(k, 0)
        tot["inr"] += r.get("inr", 0.0)
        for d, key in ((by_model, r.get("model", "?")), (by_kind, r.get("kind", "?"))):
            d[key]["requests"] += 1
            d[key]["inr"] += r.get("inr", 0.0)
    tot["inr"] = round(tot["inr"], 2)
    hit = tot["cached"] / tot["in"] if tot["in"] else 0.0
    return {"days": days, **tot, "cache_hit": round(hit, 3),
            "by_model": {k: {"requests": v["requests"], "inr": round(v["inr"], 2)}
                         for k, v in by_model.items()},
            "by_kind": {k: {"requests": v["requests"], "inr": round(v["inr"], 2)}
                        for k, v in by_kind.items()}}
