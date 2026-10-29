"""
Regression tests for failures seen in real use.

1. "busy with another request" for every question, because the background
   keep-warm pass held the request lock while faulting 14 GB back in.
2. `open_app("antigravity")` failing because the bundle is "Antigravity IDE".
3. A client disconnecting mid-answer wedging the engine.
"""
import os
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.config import SOCKET_PATH
from jarvis.daemon.client import Client
from jarvis.daemon.protocol import Reader, send

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


# ---------------------------------------------------------------- app naming
print("=== app name resolution ===")
from jarvis.tools.control import installed_apps, resolve_app

apps = installed_apps()
check("found installed applications", len(apps) > 20, len(apps))

CASES = {
    "antigravity": "Antigravity IDE",
    "chrome": "Google Chrome",
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "safari": "Safari",
    "settings": "System Settings",
}
for query, expect in CASES.items():
    if expect not in apps:
        print(f"  SKIP {query!r} ({expect} not installed)")
        continue
    got, opts = resolve_app(query)
    check(f"{query!r} -> {expect}", got == expect, f"got {got!r} / {opts}")

got, _ = resolve_app("definitely-not-an-app-zzz")
check("nonsense resolves to nothing", got is None, got)
# a wrong match is worse than no match
got, _ = resolve_app("xcode")
check("'xcode' does not mis-resolve", got in (None, "Xcode"), got)


# ---------------------------------------------------------------- daemon
def daemon_up() -> bool:
    c = Client(autostart=False)
    try:
        c.connect(timeout=3)
        st = c.ping() or {}
        c.close()
        return bool(st.get("ready"))
    except Exception:
        return False


print("\n=== engine behaviour under load ===")
if not daemon_up():
    print("  starting engine…")
    Client.spawn()
    for _ in range(180):
        if daemon_up():
            break
        time.sleep(1)
check("engine is up", daemon_up())

if daemon_up():
    # A burst of questions must all be answered. Before the fix, the keep-warm
    # pass held the lock and these came back as "busy with another request".
    print("\n=== a burst of sequential requests ===")
    results = []
    for i in range(6):
        t0 = time.time()
        p = subprocess.run(["./bin/jarvis", f"Name colour number {i + 1}."],
                           capture_output=True, text=True,
                           cwd=os.path.dirname(os.path.dirname(
                               os.path.abspath(__file__))))
        out = p.stdout
        results.append((time.time() - t0, "busy" in out.lower()
                        or "still working" in out.lower(), out))
    busy = [r for r in results if r[1]]
    check("no request was rejected as busy", not busy,
          [r[2][:120] for r in busy])
    slow = [r[0] for r in results]
    check("all answered promptly", max(slow) < 12, [round(s, 2) for s in slow])
    print(f"        timings: {[round(s, 2) for s in slow]}")

    # Two clients at once: the second should wait its turn, not be refused
    # outright, and the engine must survive either way.
    print("\n=== two clients at once ===")
    outcomes = []

    def ask(q, store):
        p = subprocess.run(["./bin/jarvis", q], capture_output=True, text=True,
                           cwd=os.path.dirname(os.path.dirname(
                               os.path.abspath(__file__))))
        store.append(p.stdout)

    a, b = [], []
    t1 = threading.Thread(target=ask, args=("Name three fruits.", a))
    t2 = threading.Thread(target=ask, args=("Name three cities.", b))
    t1.start()
    time.sleep(0.2)
    t2.start()
    t1.join(90)
    t2.join(90)
    both = (a + b)
    check("both clients got a response", len(both) == 2, len(both))
    check("engine still healthy afterwards", daemon_up())

    # A client that hangs up mid-answer must not wedge the engine.
    print("\n=== client disconnects mid-answer ===")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(SOCKET_PATH))
        send(s, {"type": "run",
                 "text": "Count slowly from one to forty in words."})
        rd = Reader(s)
        for _ in rd.messages():          # read one event, then vanish
            break
        s.close()
    except Exception as e:
        print("        (disconnect harness error:", e, ")")
    time.sleep(2)
    t0 = time.time()
    p = subprocess.run(["./bin/jarvis", "Name a colour."], capture_output=True,
                       text=True, cwd=os.path.dirname(os.path.dirname(
                           os.path.abspath(__file__))))
    dt = time.time() - t0
    check("engine answers after a client hung up",
          "busy" not in p.stdout.lower() and dt < 40, f"{dt:.1f}s {p.stdout[:120]}")
    check("engine still healthy", daemon_up())

print(f"\n{sum(R)}/{len(R)} robustness checks passed")
sys.exit(0 if all(R) else 1)
