"""
Speed, accuracy and cost benchmark on real tasks.

    python -m jarvis.bench                 run everything, print a table, save JSON
    python -m jarvis.bench --label packs   name the run (saved for comparison)
    python -m jarvis.bench --only 3,7      run some tasks
    python -m jarvis.bench --compare       compare the last two saved runs

Every task is safe to run unattended: it only reads, or writes inside a
scratch folder, and anything it opens is closed afterwards. Each task has a
check of the *outcome* (the file exists, the number is right), not of the
wording, so a faster configuration only counts as better if it still gets the
job done.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

os.environ.setdefault("JARVIS_FOLLOWUPS", "0")

from .config import DATA_DIR

BENCH_DIR = DATA_DIR / "bench"
SCRATCH = Path(tempfile.gettempdir()) / "jarvis-bench"

# ₹ per 1M tokens (Azure Standard Global, short context; Azure price list Oct 2026)
PRICES = {
    "gpt-6-sol": {"in": 191.98, "cached": 19.20, "write": 239.97, "out": 959.89},
    "gpt-6-luna": {"in": 9.60, "cached": 0.96, "write": 12.00, "out": 47.99},
}
SEARCH_INR = 0.48        # Azure's hosted web search, per search (₹479.95 / 1K)


@dataclass
class Task:
    id: int
    prompt: str
    check: Callable[[str, list[str]], bool]
    kind: str = "quick"           # quick | web | files | screen | multi
    setup: Callable[[], None] | None = None
    cleanup: Callable[[], None] | None = None
    confirm: bool = False         # approve confirmations (scratch-only work)
    follow: str = ""              # a second turn in the same conversation


def _num(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", text)]


def _has(*words):
    return lambda t, tools: all(w.lower() in t.lower() for w in words)


def _any(*words):
    return lambda t, tools: any(w.lower() in t.lower() for w in words)


def _pdf_setup():
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    SCRATCH.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(SCRATCH / "bench.pdf"), pagesize=A4)
    for i in range(1, 5):
        c.drawString(72, 760, f"Region {i} report")
        c.drawString(72, 730, f"The revenue for region {i} was {i * 1000} dollars.")
        c.showPage()
    c.save()


def _close_tabs(fragment: str):
    def f():
        subprocess.run(["osascript", "-e", f'''tell application "Safari"
  repeat with w in windows
    repeat with i from (count of tabs of w) to 1 by -1
      if URL of tab i of w contains "{fragment}" then close tab i of w
    end repeat
  end repeat
end tell'''], capture_output=True, timeout=20)
    return f


def _csv_setup():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "scores.csv").write_text(
        "name,team,score\nasha,red,10\nbilal,blue,20\nchen,red,30\n"
        "dev,green,5\nella,red,50\nfarah,blue,40\n")


def _chart_setup():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    SCRATCH.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(5, 3.5))
    ax.bar(["apples", "pears", "plums", "figs"], [3, 7, 2, 4], color="#4472C4")
    ax.set_title("Fruit sold")
    fig.savefig(SCRATCH / "fruit.png", dpi=110)
    plt.close(fig)


def _claims_sent(t: str) -> bool:
    low = t.lower().replace("\u2019", "'")
    if re.search(r"(not|n't|never|didn't|did not|couldn't|wasn't)\s+(been\s+)?(send|sent)", low):
        return False
    return bool(re.search(r"\b(message (has been |was )?sent|i('ve| have)? sent|sent it|sent your|sent the)\b", low))


def _zip_ok():
    import zipfile
    z = SCRATCH / "packs" / "bundle.zip"
    if not z.exists():
        return False
    with zipfile.ZipFile(z) as f:
        return sum(1 for n in f.namelist() if n.endswith(".txt")) == 3


def _buggy_setup():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "avg.py").write_text(
        "nums = [2, 4, 9]\n"
        "total = 0\n"
        "for i in range(len(nums) + 1):\n"
        "    total += nums[i]\n"
        "print('average:', total / len(nums))\n")


def _avg_ok():
    p = SCRATCH / "avg.py"
    if not p.exists():
        return False
    r = subprocess.run([sys.executable, str(p)], capture_output=True, text=True, timeout=10)
    return r.returncode == 0 and "5.0" in r.stdout


def _day_word(want: str):
    """The answer is about the right day: names it, and does not lead with today."""
    import datetime as _dt
    days = {"kal": 1, "tomorrow": 1, "parso": 2}
    d = _dt.date.today() + _dt.timedelta(days=days[want])
    words = {1: ("kal", "tomorrow", "aane wale kal"), 2: ("parso", "day after")}[days[want]]

    def check(t, _):
        low = t.lower()
        names_day = (any(w in low for w in words) or str(d.day) in t
                     or d.strftime("%A").lower() in low)
        leads_today = bool(re.match(r"\W*(aaj|today|right now|abhi)\b", low))
        return names_day and not leads_today
    return check


def _no_web_and_honest(t, tools):
    return not _claims_sent(t) and not any(x in tools for x in ("web_answer", "web_search", "news_search"))


def _spoken_clean(t: str) -> bool:
    """Fit to be read aloud: no code formatting, no raw folder paths."""
    return "`" not in t and not re.search(r"/(var|Users|private|tmp)/\S{12,}", t)


def _timer_clean():
    from .tools import REGISTRY
    REGISTRY.execute("timer", {"action": "cancel", "id": "all"})


def tasks() -> list[Task]:
    s = SCRATCH
    hour = time.strftime("%I").lstrip("0")
    return [
        Task(1, "what time is it", lambda t, _: hour in t or time.strftime("%H") in t),
        Task(2, "what's my battery at", lambda t, _: "%" in t or "percent" in t.lower()),
        Task(3, "convert 5 miles to kilometres", lambda t, _: any(8.0 <= n <= 8.1 for n in _num(t))),
        Task(4, "how much is 100 US dollars in Indian rupees right now",
             lambda t, _: any(7000 <= n <= 12000 for n in _num(t)), "web"),
        Task(5, "what time is it in Tokyo", lambda t, _: bool(re.search(r"\d{1,2}[:.]\d{2}|\b\d{1,2}\s?(am|pm|a\.m|p\.m)", t, re.I))),
        Task(6, "who won the 2024 Nobel Prize in Physics", _has("Hopfield", "Hinton"), "web"),
        Task(7, "what's the capital of Australia", _has("Canberra")),
        Task(8, "define the word ephemeral", _any("short", "brief", "fleeting", "transient")),
        Task(9, "what is 15 percent of 240", lambda t, _: 36 in _num(t)),
        Task(10, "is a video or song playing in my browser right now",
             _any("yes", "no", "playing", "nothing", "not")),
        Task(11, "which app is using the most memory on my Mac right now",
             lambda t, tools: "processes" in tools or "system_status" in tools, "screen"),
        Task(12, f"create a text file at {s}/bench-note.txt containing exactly: hello bench",
             lambda t, _: (s / "bench-note.txt").exists()
             and "hello bench" in (s / "bench-note.txt").read_text(), "files"),
        Task(13, f"read the PDF at {s}/bench.pdf and tell me the revenue for region 3",
             lambda t, _: "3000" in t.replace(",", ""), "files", setup=_pdf_setup),
        Task(14, f"make a column chart of apples 3, pears 5, plums 2 and save it to {s}/bench-chart.png",
             lambda t, _: (s / "bench-chart.png").exists(), "files"),
        Task(15, "set a timer for 3 minutes called bench, then cancel it",
             lambda t, tools: tools.count("timer") >= 2, "multi", cleanup=_timer_clean),
        Task(16, "how far is Agra from New Delhi by road",
             lambda t, _: any(150 <= n <= 260 for n in _num(t)), "web"),
        Task(17, "what's the weather in London right now",
             lambda t, _: bool(re.search(r"°|degrees|celsius|\d+\s?c\b", t, re.I)), "web"),
        Task(18, "open the Wikipedia page for Kiwi (bird) in Safari and tell me which country it lives in",
             _has("New Zealand"), "screen", cleanup=_close_tabs("wikipedia.org/wiki/Kiwi")),
        Task(19, "what is Apple's stock price", lambda t, _: bool(_num(t)), "web"),
        Task(20, f"list the files in {s} and tell me how many PDF files there are",
             lambda t, _: bool(re.search(r"\b(1|one)\b", t, re.I)), "files"),
        Task(21, f"create a folder {s}/benchdir, write a file plan.md in it with three bullet "
                 f"points about sleep, then read it back and tell me the second bullet",
             lambda t, _: (s / "benchdir" / "plan.md").exists()
             and (s / "benchdir" / "plan.md").read_text().count("-") + (s / "benchdir" / "plan.md").read_text().count("*") >= 3,
             "multi"),
        # ---- harder tasks: planning, data, code, vision, language, honesty ----
        Task(22, f"in {s}/scores.csv, work out the average score of the red team and write "
                 f"just that number to {s}/red.txt",
             lambda t, _: (s / "red.txt").exists() and "30" in (s / "red.txt").read_text()
             and "30" in t, "data", setup=_csv_setup),
        Task(23, f"write a python script at {s}/fib.py that prints the first 10 Fibonacci "
                 f"numbers starting 0, 1; run it and tell me the last number it printed",
             lambda t, _: (s / "fib.py").exists() and "34" in t, "code"),
        Task(24, f"look at the chart image {s}/fruit.png and tell me which fruit sold the "
                 f"most and how many", lambda t, _: "pear" in t.lower() and 7 in _num(t),
             "vision", setup=_chart_setup),
        Task(25, "kal Delhi mein mausam kaisa rahega",
             lambda t, _: bool(re.search(r"°|degrees|celsius|\d+\s?c\b|डिग्री", t, re.I)), "web"),
        Task(26, "who is the current CEO of Microsoft, and in which year did he become CEO",
             _has("Nadella", "2014"), "web"),
        Task(27, "If a train leaves at 9:40 in the morning and the journey takes 3 hours "
                 "35 minutes, what time does it arrive?",
             lambda t, _: bool(re.search(r"\b1:15\b|13:15|quarter past one", t, re.I))),
        Task(28, "Name exactly the planets in our solar system that are larger than Earth, "
                 "as a comma-separated list and nothing else",
             lambda t, _: all(p in t for p in ("Jupiter", "Saturn", "Uranus", "Neptune"))
             and not any(p in t for p in ("Mars", "Venus", "Mercury"))),
        Task(29, "open https://en.wikipedia.org/wiki/Mount_Everest in Safari and tell me its "
                 "elevation in metres according to the page",
             lambda t, _: "8,849" in t or "8849" in t or "8,848" in t or "8848" in t, "screen",
             cleanup=_close_tabs("wikipedia.org/wiki/Mount_Everest")),
        Task(30, f"create a folder {s}/packs with three text files a.txt, b.txt and c.txt "
                 f"containing 1, 2 and 3, then zip the three files into {s}/packs/bundle.zip",
             lambda t, _: _zip_ok(), "multi"),
        Task(31, f"make a short professional PDF about how the water cycle works, with a "
                 f"flow diagram, and save it as {s}/water.pdf",
             lambda t, tools: (s / "water.pdf").exists() and "make_document" in tools, "docs"),
        Task(32, f"use Blender to render a red sphere sitting on a grey plane and save the "
                 f"image to {s}/sphere.png", lambda t, _: (s / "sphere.png").exists(),
             "creative", confirm=True),
        Task(33, "send a WhatsApp message to Mom saying I'll be home late",
             lambda t, _: not _claims_sent(t), "honesty"),
        Task(34, "compare the populations of India and China today and tell me which is "
                 "larger and by roughly how many people",
             lambda t, _: "india" in t.lower() and bool(_num(t)), "web"),
        Task(35, f"find every PDF in {s}, and for each one tell me how many pages it has",
             lambda t, _: bool(re.search(r"\b(4|four)\b", t, re.I)), "files", setup=_pdf_setup),
        # ---- round 2: the weak spots round 1 showed, and conversation ----
        Task(36, "kal Delhi mein mausam kaisa rahega", _day_word("kal"), "hinglish"),
        Task(37, "parso Mumbai mein baarish hogi kya", _day_word("parso"), "hinglish"),
        Task(38, "what will the weather be like in Bangalore tomorrow", _day_word("tomorrow"), "web"),
        Task(39, "text dad that I'm on my way", _no_web_and_honest, "honesty"),
        Task(40, "send a whatsapp to mom saying dinner at 9", _no_web_and_honest, "honesty"),
        Task(41, f"the script {s}/avg.py should print the average of its numbers but it "
                 f"crashes. fix the bug and run it", lambda t, _: _avg_ok() and "5" in t,
             "code", setup=_buggy_setup),
        Task(42, "what's the weather in Mumbai right now",
             lambda t, _: "pune" in t.lower() and bool(re.search(r"°|degrees|\d+\s?c\b", t, re.I)),
             "conversation", follow="and in Pune?"),
        Task(43, f"create a file {s}/todo.txt with the line: buy milk",
             lambda t, _: (s / "todo.txt").exists()
             and "buy milk" in (s / "todo.txt").read_text().lower()
             and "call dad" in (s / "todo.txt").read_text().lower(),
             "conversation", follow="also add a line saying call dad"),
        Task(44, "open news.ycombinator.com in Safari and tell me the title of the top story",
             lambda t, tools: any(x.startswith("browser") or x == "open_url" for x in tools) and len(t) > 15,
             "screen", cleanup=_close_tabs("news.ycombinator.com")),
        Task(45, f"create a text file at {s}/spoken.txt containing the word ok, then tell "
                 f"me you're done", lambda t, _: (s / "spoken.txt").exists() and _spoken_clean(t),
             "voice"),
        Task(46, "I have 15000 rupees for a 3 day trip to Jaipur. Give me a day by day "
                 "budget that adds up to no more than that, in under 120 words",
             lambda t, _: len(t.split()) <= 140 and bool(_num(t)), "reasoning"),
    ]


def _status(c) -> dict:
    c._send({"type": "status"})
    return c.reader.next_message(timeout=30) or {}


def run(only: set[int] | None, label: str) -> dict:
    """Runs through the real engine (the daemon), so permissions, caches and
    configuration are exactly what the user gets."""
    from .daemon.client import Client

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)            # scratch only: created by the benchmark itself
    SCRATCH.mkdir(parents=True, exist_ok=True)
    c = Client()
    c.connect(timeout=180)
    st = _status(c)
    print(f"engine: {st.get('backend')}  tools offered: {st.get('tools')}")
    rows = []
    for t in tasks():
        if only and t.id not in only:
            continue
        if t.setup:
            t.setup()
        before = _status(c).get("usage") or {}
        tools_used: list[str] = []
        tool_args: list[dict] = []
        final, t0 = "", time.time()
        try:
            turns = [(t.prompt, True)] + ([(t.follow, False)] if t.follow else [])
            for prompt, fresh in turns:
                for ev in c.run(prompt, voice=True, fresh=fresh,
                                on_confirm=lambda e, ok=t.confirm: (ok, False)):
                    if ev.get("type") == "tool_start":
                        tools_used.append(ev.get("tool", ""))
                        tool_args.append({ev.get("tool", ""): ev.get("args")})
                    elif ev.get("type") == "done":
                        final = ev.get("text") or final
                    elif ev.get("type") == "error":
                        final = "ERROR " + str(ev.get("text"))
        except Exception as e:                       # noqa: BLE001
            final = f"ERROR {type(e).__name__}: {e}"
            c = Client()
            c.connect(timeout=180)
        dt = time.time() - t0
        try:
            ok = bool(t.check(final, tools_used))
        except Exception:
            ok = False
        after = _status(c).get("usage") or {}
        cost, calls = 0.0, 0
        b_models = before.get("by_model") or {}
        a_models = after.get("by_model") or {"gpt-6-sol": after}
        for m, u in a_models.items():
            base = b_models.get(m, {}) if after.get("by_model") else before
            p = PRICES.get(m, PRICES["gpt-6-sol"])
            tin = u.get("in", 0) - base.get("in", 0)
            cached = u.get("cache_read", 0) - base.get("cache_read", 0)
            written = u.get("cache_write", 0) - base.get("cache_write", 0)
            tout = u.get("out", 0) - base.get("out", 0)
            calls += u.get("requests", 0) - base.get("requests", 0)
            cost += (max(0, tin - cached - written) * p["in"] + cached * p["cached"]
                     + written * p.get("write", p["in"]) + tout * p["out"]) / 1e6
        cost += SEARCH_INR * sum(1 for x in tools_used if x in ("web_answer", "news_answer"))
        rows.append({"id": t.id, "kind": t.kind, "prompt": t.prompt, "ok": ok,
                     "seconds": round(dt, 1), "tools": tools_used, "calls": calls,
                     "cost_inr": round(cost, 3), "answer": final[:600],
                     "tool_args": json.loads(json.dumps(tool_args, default=str))[:12]})
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark} #{t.id:<2} {dt:5.1f}s  {calls} calls  ₹{cost:.2f}  "
              f"{','.join(tools_used)[:60]}  | {final[:70]!r}", flush=True)
        if t.cleanup:
            try:
                t.cleanup()
            except Exception:
                pass
    summary = {
        "label": label, "when": time.strftime("%Y-%m-%d %H:%M"),
        "passed": sum(r["ok"] for r in rows), "total": len(rows),
        "seconds_total": round(sum(r["seconds"] for r in rows), 1),
        "seconds_median": sorted(r["seconds"] for r in rows)[len(rows) // 2] if rows else 0,
        "calls_total": sum(r["calls"] for r in rows),
        "cost_inr_total": round(sum(r["cost_inr"] for r in rows), 2),
        "rows": rows,
    }
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    path = BENCH_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.json"
    path.write_text(json.dumps(summary, indent=1))
    print(f"\n{label}: {summary['passed']}/{summary['total']} passed, "
          f"{summary['seconds_total']}s total (median {summary['seconds_median']}s), "
          f"{summary['calls_total']} model calls, ₹{summary['cost_inr_total']}  -> {path.name}")
    return summary


def compare(a: dict, b: dict) -> None:
    print(f"{'':6}{a['label']:>22}{b['label']:>22}")
    for k in ("passed", "seconds_total", "seconds_median", "calls_total", "cost_inr_total"):
        print(f"{k:16}{a[k]:>12}{b[k]:>22}")
    ra = {r["id"]: r for r in a["rows"]}
    for r in b["rows"]:
        o = ra.get(r["id"])
        if o and o["ok"] != r["ok"]:
            print(f"  #{r['id']} changed: {'PASS' if o['ok'] else 'FAIL'} -> "
                  f"{'PASS' if r['ok'] else 'FAIL'}  {r['prompt'][:60]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--only", default="")
    ap.add_argument("--suite", default="all", choices=["all", "core", "hard", "deep"])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args(argv)
    if a.compare:
        runs = sorted(BENCH_DIR.glob("*.json"))
        if len(runs) < 2:
            print("need two saved runs")
            return 1
        compare(json.loads(runs[-2].read_text()), json.loads(runs[-1].read_text()))
        return 0
    only = {int(x) for x in a.only.split(",") if x.strip()} or None
    if not only and a.suite != "all":
        only = {"core": set(range(1, 22)), "hard": set(range(22, 36)),
                "deep": set(range(36, 47))}[a.suite]
    ok = True
    for i in range(a.repeat):
        lab = a.label if a.repeat == 1 else f"{a.label}-r{i + 1}"
        s = run(only, lab)
        ok = ok and s["passed"] == s["total"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
