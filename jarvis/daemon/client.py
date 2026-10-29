"""Thin client. Starts the daemon on demand and streams events back."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from typing import Callable, Iterator

from ..config import LOG_DIR, PID_FILE, SOCKET_PATH
from .protocol import Reader, send


class DaemonError(RuntimeError):
    pass


class Client:
    def __init__(self, autostart: bool = True):
        self.sock: socket.socket | None = None
        self.reader: Reader | None = None
        self.autostart = autostart

    # -- connection --------------------------------------------------------
    def connect(self, timeout: float = 90.0,
                on_status: Callable[[str], None] | None = None) -> None:
        if self._try_connect():
            return
        if not self.autostart:
            raise DaemonError("daemon is not running")
        self.spawn()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._try_connect():
                return
            time.sleep(0.25)
        raise DaemonError(
            f"daemon did not come up within {timeout:.0f}s; see {LOG_DIR / 'daemon.log'}")

    def _try_connect(self) -> bool:
        if not SOCKET_PATH.exists():
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(str(SOCKET_PATH))
            s.settimeout(None)
            self.sock, self.reader = s, Reader(s)
            return True
        except OSError:
            return False

    @staticmethod
    def _ask_app_to_spawn() -> bool:
        """
        Have the JARVIS app launch the engine when it is running. macOS gives
        a process the privacy permissions of the app that launched it, so an
        engine started from a terminal (or by an automation tool) has no
        Contacts, Calendars or Automation access, however often the user
        approved them for JARVIS - "text dad" failed exactly that way.
        """
        if os.environ.get("JARVIS_LAUNCHED_BY_APP") == "1":
            return False                 # we *are* the app's launch
        try:
            from AppKit import NSRunningApplication
            from Foundation import NSDistributedNotificationCenter
            if not NSRunningApplication.runningApplicationsWithBundleIdentifier_(
                    "com.example.jarvis.companion"):
                return False
            NSDistributedNotificationCenter.defaultCenter() \
                .postNotificationName_object_userInfo_deliverImmediately_(
                    "com.example.jarvis.startEngine", None, None, True)
        except Exception:
            return False
        # Success means an engine process appeared (a socket file can be a
        # stale leftover); connect() then waits for it to be ready.
        import subprocess
        deadline = time.time() + 6
        while time.time() < deadline:
            r = subprocess.run(["pgrep", "-f", "jarvis.daemon.server"],
                               capture_output=True, text=True)
            if r.stdout.strip():
                return True
            time.sleep(0.2)
        return False

    @staticmethod
    def spawn() -> None:
        """Launch the daemon detached so it survives this process."""
        if Client._ask_app_to_spawn():
            return
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        # The daemon starts in ~ (see cwd below), so `-m jarvis...` can no
        # longer find the package by accident of the working directory. Put the
        # package root on the path explicitly - otherwise only launches that
        # happen to go through bin/jarvis (which sets PYTHONPATH) would work.
        root = str(__import__("pathlib").Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"]
                                    if env.get("PYTHONPATH") else "")
        logf = open(LOG_DIR / "daemon.out", "ab")
        subprocess.Popen(
            [sys.executable, "-m", "jarvis.daemon.server"],
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env,
            # Home, deliberately - NOT the source tree. The daemon's cwd is
            # what bare relative paths used to resolve against, which pointed
            # every unqualified filename at JARVIS's own code.
            cwd=os.path.expanduser("~"),
        )

    # -- requests ----------------------------------------------------------
    def _send(self, obj: dict) -> None:
        if not self.sock:
            raise DaemonError("not connected")
        send(self.sock, obj)

    def _request(self, obj: dict) -> None:
        """Send, reconnecting once if the engine went away since the last
        request (restarted, updated, crashed) - a long-lived shell otherwise
        answered every later request with "lost connection"."""
        try:
            self._send(obj)
        except (OSError, DaemonError):
            self.close()
            self.connect()
            self._send(obj)

    def ping(self) -> dict | None:
        try:
            self._send({"type": "ping"})
            return self.reader.next_message(timeout=10) if self.reader else None
        except OSError:
            return None

    def status(self) -> dict | None:
        self._request({"type": "status"})
        return self.reader.next_message(timeout=10) if self.reader else None

    def grant(self, categories: list[str], clear: bool = False) -> dict | None:
        """Approve reversible categories for the engine's session (`/auto`)."""
        self._request({"type": "grant", "categories": list(categories), "clear": clear})
        return self.reader.next_message(timeout=10) if self.reader else None

    def set_backend(self, name: str) -> dict | None:
        self._send({"type": "backend", "name": name})
        # switching to the local model includes a 14 GB load
        return self.reader.next_message(timeout=300) if self.reader else None

    def reset(self) -> None:
        self._request({"type": "reset"})
        if self.reader:
            self.reader.next_message(timeout=120)

    def interrupt(self) -> None:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)              # a wedged engine must not freeze Ctrl-C
            s.connect(str(SOCKET_PATH))
            send(s, {"type": "interrupt"})
            s.close()
        except OSError:
            pass

    def shutdown(self) -> bool:
        try:
            self._send({"type": "shutdown"})
            return True
        except Exception:
            return False

    def run(self, text: str, voice: bool = False, fresh: bool = False,
            on_confirm: Callable[[dict], tuple[bool, bool]] | None = None
            ) -> Iterator[dict]:
        """Yield event dicts until 'done' or 'error'."""
        self._request({"type": "run", "text": text, "voice": voice, "fresh": fresh})
        assert self.reader
        for ev in self.reader.messages():
            t = ev.get("type")
            if t == "confirm":
                approve, remember = (on_confirm(ev) if on_confirm else (False, False))
                self._send({"type": "confirm_reply", "approve": approve,
                            "remember": remember})
                continue
            yield ev
            if t in ("done", "error"):
                return
        # The stream ended with no answer: the engine stopped mid-request. Say
        # so (this used to end silently, printing nothing at all) and drop the
        # dead connection so the next request reconnects.
        self.close()
        yield {"type": "error", "text": "lost connection to the engine - it stopped "
                                        "in the middle of the request (see "
                                        f"{LOG_DIR / 'daemon.log'})"}

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = self.reader = None


def daemon_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None
