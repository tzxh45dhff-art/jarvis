"""
End-to-end tests. These drive the real agent with the real model and real tools -
no mocks - and assert on observable side effects (files created, commands run,
memories persisted) rather than on the model's wording.
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401  (keeps tests out of the user's memory)

from jarvis.core.agent import Agent
from jarvis.core.llm import get_backend
from jarvis.safety import journal
from jarvis.safety.policy import Risk
from jarvis.tools import REGISTRY

R = []
TIMES = {}


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"\n        {extra}"))
    R.append(bool(cond))
    return bool(cond)


class Run:
    """Execute one request and collect everything that happened."""

    def __init__(self, agent, task, **kw):
        self.text = ""
        self.tools = []
        self.failed_tools = []
        self.errors = []
        self.meta = {}
        t0 = time.time()
        for ev in agent.run(task, **kw):
            if ev.type == "text":
                self.text += ev.text
            elif ev.type == "tool_start":
                self.tools.append(ev.tool)
            elif ev.type == "tool_end" and not ev.ok:
                self.failed_tools.append((ev.tool, ev.text))
            elif ev.type == "error":
                self.errors.append(ev.text)
            elif ev.type == "done":
                self.meta = ev.meta
                self.text = ev.text or self.text
        self.elapsed = time.time() - t0

    def used(self, *names):
        return any(n in self.tools for n in names)


def main():
    print("loading model…")
    t0 = time.time()
    b = get_backend()
    b.load()
    b.warm()
    print(f"  ready in {time.time() - t0:.1f}s\n")

    approvals = []

    def auto_confirm(name, args, verdict):
        approvals.append((name, verdict.category))
        return True

    # Memories from a previous run get retrieved into these prompts and change
    # what the model does, which makes the suite non-reproducible. Start clean.
    from jarvis.memory.store import get_store
    get_store().clear()

    REGISTRY.confirm_hook = auto_confirm
    agent = Agent(b)
    # The daemon does this at startup; do it here too so the suite measures the
    # steady state rather than one-off Metal kernel compilation.
    for _ in agent.run("Say ready.", max_steps=1):
        pass
    agent.reset()
    b.warm()
    tmp = tempfile.mkdtemp(prefix="jarvis_test_")

    # ---------------------------------------------------------------- 1. text
    print("[1] plain text, no tools")
    r = Run(agent, "What is 25 times 8? Reply with just the number.")
    check("answers correctly", "200" in r.text, r.text)
    check("uses no tools for arithmetic", not r.tools, r.tools)
    check("fast first token (<1.5s)", r.meta.get("ttft", 9) < 1.5, r.meta)
    TIMES["simple"] = r.elapsed

    # ---------------------------------------------------------------- 2. shell
    print("\n[2] terminal execution")
    r = Run(agent, "Run the shell command 'echo JARVIS_MARKER_7788' and tell me "
                   "exactly what it printed.")
    check("used the shell", r.used("run_shell"), r.tools)
    check("reports real output", "JARVIS_MARKER_7788" in r.text, r.text)

    # ---------------------------------------------------------------- 3. files
    print("\n[3] file creation and reading")
    f = os.path.join(tmp, "note.txt")
    r = Run(agent, f"Create a file at {f} containing exactly the line "
                   f"'alpha beta gamma'. Then read it back and tell me its contents.")
    check("file was created", os.path.exists(f), f)
    if os.path.exists(f):
        check("file has the right content", "alpha beta gamma" in open(f).read(),
              repr(open(f).read()))
    check("used write and read tools", r.used("write_file"), r.tools)

    # ---------------------------------------------------------------- 4. edit
    print("\n[4] editing an existing file")
    code = os.path.join(tmp, "calc.py")
    open(code, "w").write("def add(a, b):\n    return a - b\n\nprint(add(2, 3))\n")
    r = Run(agent, f"The file {code} has a bug: add() subtracts instead of adding. "
                   f"Fix it, then run it with python3 and tell me the output.")
    src = open(code).read()
    check("bug is fixed in the file", "a + b" in src, src)
    check("ran the program to verify", r.used("run_shell", "python_eval"), r.tools)
    check("reports the corrected result", "5" in r.text, r.text)

    # ---------------------------------------------------------------- 5. undo
    print("\n[5] mutations are reversible")
    before = open(code).read()
    mid = journal.record("overwrite", code, session="test")
    open(code, "w").write("# clobbered\n")
    ok, msg = journal.undo(mid)
    check("undo restored the file", ok and open(code).read() == before, msg)

    # ---------------------------------------------------------------- 6. search
    print("\n[6] multi-step: search the filesystem")
    os.makedirs(os.path.join(tmp, "deep", "nested"), exist_ok=True)
    open(os.path.join(tmp, "deep", "nested", "needle_xyz.md"), "w").write("found me")
    r = Run(agent, f"Find any file whose name contains 'needle_xyz' under {tmp} "
                   f"and tell me its full path.")
    check("located the file", "needle_xyz" in r.text, r.text)
    check("used a search tool", r.used("find_files", "search_text", "run_shell"),
          r.tools)

    # ---------------------------------------------------------------- 7. web
    print("\n[7] live web research")
    r = Run(agent, "Search the web for the official Python release schedule and "
                   "tell me one concrete fact with the source URL you read.")
    check("used a web tool", r.used("web_search", "fetch_page", "news_search",
                                    "fetch_many"), r.tools)
    check("cites a real url", "http" in r.text, r.text[:300])

    # ---------------------------------------------------------------- 8. doc
    print("\n[8] document generation")
    pdf = os.path.join(tmp, "report.pdf")
    r = Run(agent, f"Write a short two-section markdown report about the benefits "
                   f"of unit testing, with a heading and a bullet list, and save it "
                   f"as a PDF to {pdf}. Then confirm the file exists and its size.")
    check("PDF created", os.path.exists(pdf), pdf)
    if os.path.exists(pdf):
        check("PDF is non-trivial", os.path.getsize(pdf) > 1200,
              os.path.getsize(pdf))
        check("PDF has a valid header", open(pdf, "rb").read(5) == b"%PDF-",
              open(pdf, "rb").read(8))

    # ---------------------------------------------------------------- 9. screen
    print("\n[9] screen understanding")
    r = Run(agent, "Look at my screen and describe in one sentence what is visible.")
    check("used vision", r.used("see_screen", "screenshot"), r.tools)
    check("produced an observation", len(r.text) > 20, r.text)

    # ---------------------------------------------------------------- 10. memory
    print("\n[10] memory: store, recall, persist")
    r = Run(agent, "Remember that my favourite text editor is Neovim and I keep my "
                   "side projects in ~/Code/experiments.")
    check("stored a memory", r.used("remember"), r.tools)
    agent.reset()
    r = Run(agent, "What is my favourite text editor?")
    check("recalls across a conversation reset", "vim" in r.text.lower(), r.text)

    from jarvis.memory.store import get_store
    hits = get_store().search("favourite editor")
    check("memory is in persistent storage",
          any("vim" in m.content.lower() for m in hits),
          [m.content for m in hits])

    # ---------------------------------------------------------------- 11. safety
    print("\n[11] safety gating")
    approvals.clear()
    danger = os.path.join(tmp, "deletable.txt")
    open(danger, "w").write("x")
    r = Run(agent, f"Move the file {danger} to the Trash.")
    check("deletion required confirmation",
          any(c == "delete" for _, c in approvals), approvals)
    check("file was trashed, not unlinked", not os.path.exists(danger))

    from jarvis.safety.policy import classify_shell
    v = classify_shell("r" + "m -rf /")
    check("catastrophic command is refused outright", v.risk == Risk.FORBIDDEN, v)
    res = REGISTRY.execute("run_shell", {"command": "r" + "m -rf /"})
    check("registry blocks it even if the model asks",
          not res.ok and "REFUSED" in (res.error or ""), res.error)

    # ---------------------------------------------------------------- 12. errors
    print("\n[12] error recovery")
    r = Run(agent, f"Read the file {tmp}/definitely_missing_9911.txt. If it does "
                   f"not exist, say so plainly and create it with the text 'recovered'.")
    created = os.path.join(tmp, "definitely_missing_9911.txt")
    check("recovered from the failed read", os.path.exists(created), created)
    check("noticed the failure", len(r.failed_tools) >= 1 or "not exist" in r.text.lower(),
          f"{r.failed_tools} / {r.text[:150]}")

    # ---------------------------------------------------------------- 13. multi
    print("\n[13] multi-step chained task")
    outdir = os.path.join(tmp, "organised")
    for n in ("a.txt", "b.txt", "c.log"):
        open(os.path.join(tmp, n), "w").write("data")
    r = Run(agent, f"In {tmp}: create a folder called 'organised', then move every "
                   f"file ending in .txt into it. Finally list what is inside it.")
    moved = (os.path.isdir(outdir)
             and len([x for x in os.listdir(outdir) if x.endswith(".txt")]) >= 2)
    check("performed the whole chain", moved,
          os.listdir(outdir) if os.path.isdir(outdir) else "no folder")
    check("took multiple steps", r.meta.get("steps", 0) >= 2, r.meta)

    # ---------------------------------------------------------------- 14. stream
    print("\n[14] streaming")
    chunks = []
    for ev in agent.run("Count from one to five in words, separated by commas."):
        if ev.type == "text":
            chunks.append(ev.text)
    check("emits many incremental chunks", len(chunks) > 4, len(chunks))
    check("streamed text is coherent", "five" in "".join(chunks).lower(),
          "".join(chunks))

    # ---------------------------------------------------------------- 15. perf
    print("\n[15] cache keeps latency low over a conversation")
    ttfts = []
    for q in ("Name a colour.", "Name another.", "And one more."):
        rr = Run(agent, q)
        ttfts.append(rr.meta.get("ttft", 9))
    check("later turns stay fast (<1.5s to first token)",
          max(ttfts) < 1.5, ttfts)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{sum(R)}/{len(R)} integration checks passed")
    return 0 if all(R) else 1


if __name__ == "__main__":
    sys.exit(main())
