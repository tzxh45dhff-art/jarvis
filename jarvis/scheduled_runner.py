"""
Run one scheduled task, unattended: ``python -m jarvis.scheduled_runner <id>``.

Started by launchd. It asks the engine (starting it if needed), declines any
confirmation - nobody is there to give one - and delivers the answer as a macOS
notification. The prompt is wrapped so the engine knows this is an unattended
run and keeps to looking things up and summarising.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from .config import DATA_DIR

TASK_DIR = DATA_DIR / "tasks"
AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "com.example.jarvis.task."

WRAPPER = (
    "[This is a scheduled task running unattended; nobody is at the keyboard. "
    "Only look things up and summarise. Do not change, send, delete, install "
    "or open anything. Reply in at most three short sentences suitable for a "
    "notification.]\n\n"
)


def notify(title: str, body: str) -> None:
    script = ('on run argv\n display notification (item 2 of argv) with title (item 1 of argv)'
              ' sound name "Glass"\nend run')
    subprocess.run(["/usr/bin/osascript", "-e", script, title, body[:230]],
                   capture_output=True, timeout=15)


def run_task(task_id: str, client_factory=None) -> str:
    meta_path = TASK_DIR / f"{task_id}.json"
    if not meta_path.exists():
        return ""
    task = json.loads(meta_path.read_text())
    if client_factory is None:
        from .daemon.client import Client
        client_factory = lambda: Client()            # noqa: E731
    answer, error = "", ""
    try:
        c = client_factory()
        c.connect(timeout=180)
        for ev in c.run(WRAPPER + task["prompt"], voice=True, fresh=False,
                        on_confirm=lambda e: (False, False)):
            if ev.get("type") == "done":
                answer = (ev.get("text") or "").strip()
            elif ev.get("type") == "error":
                error = ev.get("text", "error")
        c.close()
    except Exception as e:                           # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    result = answer or (f"Could not run: {error}" if error else "No result.")
    (TASK_DIR / f"{task_id}.last.txt").write_text(
        f"{time.strftime('%Y-%m-%d %H:%M')}\n{result}\n")
    notify("JARVIS", result)
    if task.get("one_shot"):
        import os
        plist = AGENTS / f"{LABEL_PREFIX}{task_id}.plist"
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
                       capture_output=True, timeout=15)
        plist.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m jarvis.scheduled_runner <task id>")
        return 2
    run_task(argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
