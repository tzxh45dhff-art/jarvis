"""
Stopping, against the running engine (needs `jarvis start`; uses the model).

Stop must free the engine within a second even when a tool is blocked, a new
request must never be refused because an earlier one is stuck or its app quit,
and anything a stopped request launched must be ended.
"""
import sys, time, threading, subprocess, socket
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis.daemon.client import Client
from jarvis.config import SOCKET_PATH
from jarvis.daemon.protocol import send

def run(prompt, approve=True, out=None, fresh=True):
    c = Client(); c.connect(timeout=60)
    t0 = time.time(); final = ""
    for ev in c.run(prompt, voice=True, fresh=fresh, on_confirm=lambda e: (approve, False)):
        if ev.get("type") in ("done", "error"):
            final = f"{ev.get('type')}: {ev.get('text')}"
    if out is not None: out.append((round(time.time() - t0, 1), final))
    return final

def interrupt():
    s = socket.socket(socket.AF_UNIX); s.connect(str(SOCKET_PATH)); send(s, {"type": "interrupt"}); s.close()

print("TEST 1: stop a task blocked in a slow step")
res = []
th = threading.Thread(target=run, args=("use python_eval to run exactly this code and nothing else: import time; time.sleep(60); print('woke')",), kwargs={"out": res})
th.start(); time.sleep(8)
t = time.time(); interrupt(); th.join(30)
stop1 = time.time()-t
print(f"   stop took {stop1:.2f}s ->", res)

print("TEST 2: 'hello' right after stopping")
t = time.time(); r = run("hello"); hello = r; print(f"   {time.time()-t:.1f}s ->", r[:100])

print("TEST 3: app quits mid-task, then a new request")
c = Client(); c.connect(timeout=30)
c._send({"type": "run", "text": "use python_eval to run: import time; time.sleep(60)", "voice": True, "fresh": True})
time.sleep(8); c.sock.close()            # the app quits
t = time.time(); r = run("what's 2 plus 2"); quit_ok = r; print(f"   {time.time()-t:.1f}s ->", r[:100])

print("TEST 4: a stopped shell command is really ended")
res = []
th = threading.Thread(target=run, args=("run the shell command: sleep 247",), kwargs={"out": res})
th.start(); time.sleep(10)
before = subprocess.run(["pgrep", "-f", "sleep 247"], capture_output=True, text=True).stdout.split()
t = time.time(); interrupt(); th.join(30)
time.sleep(1)
after = subprocess.run(["pgrep", "-f", "sleep 247"], capture_output=True, text=True).stdout.split()
print(f"   stop took {time.time()-t:.2f}s; sleep processes before={len(before)} after={len(after)} ->", res)

stop4 = time.time() - t
checks = [
    ("stop frees a blocked request within 1.5 s", stop1 < 1.5),
    ("the next request is answered, not refused", hello.startswith("done") and "busy" not in hello and "still working" not in hello),
    ("a request whose app quit does not block the next", quit_ok.startswith("done") and "4" in quit_ok or "four" in quit_ok.lower()),
    ("a stopped shell command is ended", not after),
]
for n, ok in checks:
    print(("  PASS " if ok else "  FAIL ") + n)
print(f"{sum(ok for _, ok in checks)}/{len(checks)} stop checks passed")
sys.exit(0 if all(ok for _, ok in checks) else 1)
