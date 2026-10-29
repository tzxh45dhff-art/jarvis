"""Terminal execution. Every command is classified before it runs."""
from __future__ import annotations

import os
import subprocess
import threading

from ..config import CONFIG
from ..safety.policy import Risk, classify_shell
from .registry import REGISTRY, ToolResult

MAX_OUT = 30_000
_BG: dict[str, subprocess.Popen] = {}
_BG_OUT: dict[str, list[str]] = {}


def _trunc(s: str, n: int = MAX_OUT) -> str:
    if len(s) <= n:
        return s
    return s[: n // 2] + f"\n... [{len(s) - n} chars elided] ...\n" + s[-n // 2:]


def _fail_reason(rc: int, stdout: str, stderr: str, n: int = 1500) -> str:
    """Why a command failed, in the error itself: a failed result reaches the
    model as its error string only, so a bare "exit 1" hid every traceback and
    the model re-ran or edited blind."""
    if rc < 0:
        head = (f"killed by signal {-rc}"
                + (" (stopped - the request was cancelled or the process was "
                   "terminated)" if -rc in (9, 15) else ""))
    else:
        head = f"exit {rc}"
    tail = (stderr or "").strip() or (stdout or "").strip()
    if not tail:
        return head
    if len(tail) > n:
        tail = "..." + tail[-n:]
    return f"{head}: {tail}"


@REGISTRY.tool(
    "run_shell",
    "Run a shell command on macOS and return stdout/stderr. Use for builds, tests, "
    "git, package managers, and any CLI work. Destructive commands are gated.",
    {"type": "object", "properties": {
        "command": {"type": "string", "description": "The shell command"},
        "cwd": {"type": "string", "description": "Working directory"},
        "timeout": {"type": "integer", "description": "Seconds before giving up (default 120)"}},
     "required": ["command"]},
    risk=Risk.MEDIUM, category="generic",
    assess=lambda a: classify_shell(a.get("command", "")),
    parallel_safe=False, slow=True)
def run_shell(command: str, cwd: str = "", timeout: int = 120):
    wd = os.path.expanduser(cwd) if cwd else str(CONFIG.workspace)
    if not os.path.isdir(wd):
        wd = str(CONFIG.workspace)
    try:
        r = subprocess.run(command, shell=True, cwd=wd, capture_output=True,
                           text=True, timeout=max(1, min(timeout, 900)),
                           executable="/bin/zsh")
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"Timed out after {timeout}s. "
                                       f"For long jobs use run_background.")
    out = _trunc((r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else ""))
    ok = r.returncode == 0
    return ToolResult(ok, content={"exit_code": r.returncode, "output": out.strip(),
                                   "cwd": wd},
                      error="" if ok else _fail_reason(r.returncode, r.stdout, r.stderr),
                      display=f"$ {command[:70]}  (exit {r.returncode})")


@REGISTRY.tool(
    "run_background",
    "Start a long-running command in the background (servers, builds, watchers). "
    "Returns a job id; poll it with check_job.",
    {"type": "object", "properties": {
        "command": {"type": "string"}, "cwd": {"type": "string"}},
     "required": ["command"]},
    risk=Risk.MEDIUM, category="generic",
    assess=lambda a: classify_shell(a.get("command", "")), parallel_safe=False)
def run_background(command: str, cwd: str = ""):
    wd = os.path.expanduser(cwd) if cwd else str(CONFIG.workspace)
    job = f"job{len(_BG) + 1}"
    p = subprocess.Popen(command, shell=True, cwd=wd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, executable="/bin/zsh")
    _BG[job] = p
    _BG_OUT[job] = []

    def pump():
        try:
            for line in p.stdout:              # type: ignore[union-attr]
                _BG_OUT[job].append(line.rstrip())
                if len(_BG_OUT[job]) > 4000:
                    del _BG_OUT[job][:2000]
        except Exception:
            pass

    threading.Thread(target=pump, daemon=True).start()
    return ToolResult(True, content={"job_id": job, "pid": p.pid},
                      display=f"started {job} (pid {p.pid}): {command[:50]}")


@REGISTRY.tool(
    "check_job",
    "Check output and status of a background job started with run_background.",
    {"type": "object", "properties": {
        "job_id": {"type": "string"}, "lines": {"type": "integer"}},
     "required": ["job_id"]},
    risk=Risk.SAFE, category="read")
def check_job(job_id: str, lines: int = 60):
    p = _BG.get(job_id)
    if p is None:
        return ToolResult(False, error=f"No such job: {job_id}. Known: {list(_BG)}")
    rc = p.poll()
    out = "\n".join(_BG_OUT.get(job_id, [])[-lines:])
    return ToolResult(True, content={"job_id": job_id, "running": rc is None,
                                     "exit_code": rc, "output": _trunc(out, 12000)},
                      display=f"{job_id}: {'running' if rc is None else f'exited {rc}'}")


@REGISTRY.tool(
    "stop_job",
    "Terminate a background job started with run_background.",
    {"type": "object", "properties": {"job_id": {"type": "string"}},
     "required": ["job_id"]},
    risk=Risk.MEDIUM, category="generic", parallel_safe=False)
def stop_job(job_id: str):
    p = _BG.get(job_id)
    if p is None:
        return ToolResult(False, error=f"No such job: {job_id}")
    p.terminate()
    return ToolResult(True, content={"job_id": job_id, "stopped": True},
                      display=f"stopped {job_id}")


@REGISTRY.tool(
    "python_eval",
    "Run a short Python snippet in a subprocess and capture stdout. "
    "Use for arithmetic, data munging, and quick verification. Not for long jobs.",
    {"type": "object", "properties": {
        "code": {"type": "string", "description": "Python source; print() what you need"}},
     "required": ["code"]},
    risk=Risk.LOW, category="compute", slow=False)
def python_eval(code: str):
    import sys
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return ToolResult(False, error="python_eval timed out (60s)")
    out = _trunc((r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else ""), 12000)
    return ToolResult(r.returncode == 0, content={"output": out.strip()},
                      error="" if r.returncode == 0 else _fail_reason(r.returncode, r.stdout, r.stderr),
                      display="ran python snippet")
