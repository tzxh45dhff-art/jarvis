"""
Resident daemon.

The model is 14 GB and takes ~17 s to load and warm. Keeping it resident is what
makes `jarvis "do the thing"` feel instant instead of costing 17 s every time,
and it is what lets wake-word listening run continuously in the background.
"""
from __future__ import annotations

import logging
import os

# The embedding model is already on disk; without this the engine asked
# huggingface.co about it ~30 times at every start (about 7 s). Only when it is
# cached, so a fresh machine can still download it once.
_HUB = os.path.expanduser("~/.cache/huggingface/hub")
if os.path.isdir(os.path.join(
        _HUB, "models--sentence-transformers--all-MiniLM-L6-v2")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
import queue
import signal
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

from ..config import CONFIG, LOG_DIR, PID_FILE, RUN_DIR, SOCKET_PATH
from ..core.agent import Agent
from ..core.llm import backend_name, get_backend, is_remote, resolve_kind
from ..tools import REGISTRY
from ..tools.registry import CURRENT_TURN
from .protocol import Reader, send as _send


def send(conn, obj) -> bool:
    """Write to a client, tolerating one that has already gone away."""
    try:
        _send(conn, obj)
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False

log = logging.getLogger("jarvis.daemon")

LOCK_FILE = RUN_DIR / "jarvisd.lock"
# What `/auto` may approve for a session: reversible actions only.
GRANTABLE = frozenset({"control", "ui_input", "generic", "overwrite", "install",
                       "system_pref"})


def acquire_singleton_lock():
    """
    Guarantee one engine per machine.

    The model is 14 GB. Two instances do not merely waste memory, they exceed
    it and the machine starts swapping. An advisory flock is used rather than a
    pid file alone because it is released automatically if the process is
    killed, so a crash cannot leave the engine permanently unstartable.
    """
    import fcntl
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh          # keep the handle alive for the process lifetime


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / "daemon.log")],
    )


def is_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return SOCKET_PATH.exists()
    except (ValueError, ProcessLookupError, PermissionError):
        return False


class Daemon:
    def __init__(self) -> None:
        from .. import code_stamp
        self.code = code_stamp()            # what this process was started with
        self.backend = get_backend()
        self.agent = Agent(self.backend)
        self.ready = threading.Event()
        self.status = "starting"
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._stop_listening = threading.Event()
        self._stt = None
        self._listen_lock = threading.Lock()
        self._request_pending = threading.Event()
        self._lock_fh = None
        self._shutting_down = threading.Event()
        self._shutdown_done = threading.Event()
        # The request in progress: {"id", "cancel", "started", "conn"}.
        self._turn: dict | None = None
        self._turn_seq = 0

    # -- lifecycle ---------------------------------------------------------
    def _load(self) -> None:
        try:
            self.status = "loading model"
            t0 = time.time()
            self.backend.load()
            log.info("model loaded in %.1fs", time.time() - t0)
            try:
                # Decide up front whether browser_js is offered, so the tool
                # list (and with it the provider's prompt cache) is stable
                # from the first request instead of changing mid-session.
                from ..tools import browser as _browser, osa as _osa
                if _osa.app_running("Safari"):
                    _browser.run_js("1", "Safari", timeout=5)
            except Exception:
                pass
            self.status = "warming cache"
            w = self.backend.warm()
            log.info("prefix cache warmed in %.1fs", w)
            self.status = "warming speech"
            try:
                if not CONFIG.voice.enabled:
                    raise RuntimeError("voice is off (JARVIS_VOICE_ENABLED=1 to "
                                       "enable) - speech models not loaded")
                # Whisper's first call pays model load and Metal compilation.
                # Spend it here so the first thing the user says does not.
                import numpy as np
                from ..voice.stt import WhisperSTT
                from ..voice.tts import Speaker
                self._stt = WhisperSTT()
                t1 = time.time()
                self._stt.transcribe(np.zeros(16000, dtype=np.float32), 16000)
                log.info("speech recogniser warmed in %.1fs", time.time() - t1)
                sp = Speaker()
                sp.warm()
                self._speaker = sp
            except Exception as e:
                log.warning("voice warmup skipped: %s", e)
            self.status = "warming screen OCR"
            try:
                # The first Vision request loads Apple's text-recognition
                # models and costs ~4 s; afterwards OCR is ~0.3 s. Pay it here.
                from ..tools.vision_ground import ocr_screen
                t1 = time.time()
                ocr_screen(force=True)
                log.info("vision OCR warmed in %.1fs", time.time() - t1)
            except Exception as e:
                log.warning("ocr warmup skipped: %s", e)
            self.status = "warming memory"
            try:
                from ..memory.store import get_store
                get_store().search("warmup")
            except Exception as e:
                log.warning("memory warmup: %s", e)

            # Restoring the KV cache skips the prefill but not Metal kernel
            # compilation, which happens on the first forward pass and cost the
            # user's first question ~7 s. Spend it here instead, before anyone
            # is waiting.
            # Restoring the KV cache skips the prefill but not Metal kernel
            # compilation, which happens on the first forward pass. Measured, an
            # unwarmed first question costs ~10.7 s. Warming through the real
            # agent path - same prompt shape, same tool schemas, same memory
            # preamble - compiles the kernels that request will actually use.
            # Only the local model needs this: Metal kernels compile on the
            # first forward pass and cost the user's first question ~10 s. A
            # remote backend has no kernels and no resident weights, so running
            # a throwaway turn against it would just spend money.
            if not is_remote(self.backend):
                self.status = "warming kernels"
                try:
                    t0 = time.time()
                    for _ in self.agent.run("Say ready.", max_steps=1):
                        pass
                    self.agent.reset()
                    self.backend.warm()          # discard the warmup conversation
                    self.backend.touch_weights()  # every expert resident
                    log.info("kernels warmed in %.1fs", time.time() - t0)
                except Exception as e:
                    log.warning("kernel warmup: %s", e)

            self.status = "ready"
            self.ready.set()
            log.info("daemon ready (%s)", backend_name(self.backend))
            try:
                from ..memory.profile import Watcher
                self._watcher = Watcher()
                self._watcher.start()
                from ..memory.clips import ClipWatcher
                self._clips = ClipWatcher()
                self._clips.start()
                from ..memory.profile import import_contact_dates
                threading.Thread(target=import_contact_dates, daemon=True,
                                 name="contact-dates").start()
            except Exception as e:                       # noqa: BLE001
                log.warning("profile watcher not started: %s", e)
            if not is_remote(self.backend):
                threading.Thread(target=self._keep_warm, daemon=True).start()
        except Exception:
            self.status = "failed: " + traceback.format_exc(limit=2)
            log.exception("startup failed")
            self.ready.set()

    def _keep_warm(self) -> None:
        """
        Keep the weights resident so an idle assistant is still an instant one.

        Two rules, both learned the hard way:

        * **It must never make a user wait.** An earlier version held the
          request lock while it worked. Under memory pressure a pass took over a
          minute, so every question during that window was answered with
          "busy with another request". It now yields the moment a request
          arrives and holds nothing a request needs.
        * **It must back off when the machine is thrashing.** If a pass is slow,
          the pages are being evicted faster than they can be touched, and
          hammering every 60 s makes that worse rather than better.
        """
        interval = 60.0
        while not self._stop.wait(interval):
            if not self.ready.is_set() or self._request_pending.is_set():
                continue
            try:
                dt = self.backend.touch_weights(cancel=self._request_pending)
            except Exception as e:
                log.warning("keep-warm failed: %s", e)
                continue
            if self._request_pending.is_set():
                interval = 60.0            # yielded: nothing learned, retry soon
                continue
            if dt > 5.0:
                # thrashing - stop fighting it, and let the machine settle
                interval = min(interval * 2, 600.0)
                log.info("keep-warm slow (%.1fs); backing off to %.0fs",
                         dt, interval)
            elif dt > 1.0:
                interval = 60.0
                log.debug("keep-warm faulted pages back in (%.1fs)", dt)
            else:
                interval = 60.0

    def serve(self) -> None:
        _setup_logging()
        self._lock_fh = acquire_singleton_lock()
        if self._lock_fh is None:
            existing = None
            try:
                existing = int(PID_FILE.read_text().strip())
            except Exception:
                pass
            msg = ("another JARVIS engine is already running"
                   + (f" (pid {existing})" if existing else "")
                   + "; refusing to load a second copy of the model")
            log.error(msg)
            print(msg, file=sys.stderr)
            raise SystemExit(3)
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)          # this socket controls the machine
        self._sock.listen(8)
        PID_FILE.write_text(str(os.getpid()))
        for s in (signal.SIGTERM, signal.SIGINT):
            signal.signal(s, lambda *_: self.shutdown())
        # `kill -USR1 <engine pid>` writes every thread's stack to daemon.out:
        # a frozen engine can be diagnosed instead of only restarted.
        try:
            import faulthandler
            faulthandler.register(signal.SIGUSR1, all_threads=True)
        except Exception:
            pass
        threading.Thread(target=self._load, daemon=True).start()
        log.info("listening on %s", SOCKET_PATH)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        if self._shutting_down.is_set():
            self._shutdown_done.wait(12.0)        # let the learner finish saving
        self._cleanup()

    def shutdown(self) -> None:
        # Stop taking requests first: one that arrived while the engine was
        # still saving below was accepted and then dropped ("Lost the
        # connection") - the app now waits for the restarted engine instead.
        if self._shutting_down.is_set():
            return
        self._shutting_down.set()
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        try:
            SOCKET_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        # Let the next engine start right away (a restart used to wait for
        # this one to finish saving, or fail to start at all).
        try:
            if self._lock_fh:
                self._lock_fh.close()
                self._lock_fh = None
        except Exception:
            pass
        # What the user said in the last few turns is still waiting to be
        # learned; a restart must not throw it away (bounded: a few seconds).
        try:
            t = threading.Thread(target=self.agent._learner.flush, daemon=True)
            t.start()
            t.join(8.0)
        except Exception:
            pass
        self._shutdown_done.set()

    def _cleanup(self) -> None:
        # Only remove what is still ours: after shutdown() released the lock a
        # new engine may already own the socket, pid and lock files, and
        # deleting them left it running but unreachable.
        still_owner = bool(getattr(self, "_lock_fh", None))
        try:
            if still_owner:
                self._lock_fh.close()
        except Exception:
            pass
        if still_owner:
            for p in (SOCKET_PATH, LOCK_FILE):
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
        try:
            if PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        log.info("daemon stopped")

    # -- request handling --------------------------------------------------
    def _handle(self, conn: socket.socket) -> None:
        reader = Reader(conn)
        try:
            for msg in reader.messages():
                kind = msg.get("type")
                if kind == "ping":
                    send(conn, {"type": "pong", "status": self.status,
                                "ready": self.ready.is_set(),
                                "code": self.code, "busy": self._busy.locked()})
                elif kind == "status":
                    send(conn, {"type": "status", "status": self.status,
                                "ready": self.ready.is_set(),
                                "tools": len(REGISTRY.names()),
                                "backend": backend_name(self.backend),
                                "kind": getattr(self.backend, "_kind", "local"),
                                "usage": getattr(self.backend, "usage", {}),
                                "mouse_keyboard": self._input_status(),
                                "turns": len(self.agent.messages)})
                elif kind == "backend":
                    send(conn, self._switch_backend(msg.get("name", "")))
                elif kind == "shutdown":
                    send(conn, {"type": "bye"})
                    self.shutdown()
                    return
                elif kind == "reset":
                    self._reset_conversation()
                    send(conn, {"type": "reset_ok"})
                elif kind == "confirm_reply":
                    # The answer to a prompt from a request that has already
                    # ended (stopped, or timed out). Answering "unknown request"
                    # put an error on the stream, which the client then read as
                    # the outcome of its *next* request.
                    continue
                elif kind == "grant":
                    # `/auto`: reversible categories approved for the session.
                    # It has to happen here - the CLI's own registry is not the
                    # one that asks. Sending, deleting and the like never qualify.
                    if msg.get("clear"):
                        REGISTRY.granted.clear()
                    else:
                        REGISTRY.granted.update(
                            {str(c) for c in msg.get("categories") or []} & GRANTABLE)
                    send(conn, {"type": "ok", "granted": sorted(REGISTRY.granted)})
                elif kind == "interrupt":
                    was_busy = self._busy.locked()
                    self._cancel_turn("stopped by the user")
                    self._request_pending.clear()
                    send(conn, {"type": "interrupted", "was_busy": was_busy})
                elif kind == "suggest":
                    # Never queued behind a request and never during one: a
                    # suggestion is only worth anything in the moment.
                    from .. import proactive
                    sug = None if self._busy.locked() else proactive.suggest(msg.get("event") or {})
                    send(conn, sug or {"type": "none"})
                elif kind == "get_settings":
                    from ..memory import profile as _p
                    send(conn, {"type": "settings", "settings": {
                        "suggestions": _p._setting("suggestions", "suggest"),
                        "learning": _p._setting("learning", "on"),
                        "watching": _p._setting("watching", "on"),
                        **{f"p_{k}": v for k, v in _p.personality().items()}}})
                elif kind == "set_setting":
                    from ..memory import profile as _p
                    key, val = str(msg.get("key", "")), str(msg.get("value", ""))
                    ok = (key == "suggestions" and val in ("off", "suggest", "auto")) or \
                         (key in ("learning", "watching") and val in ("on", "off")) or \
                         (key.startswith("p_") and val in _p.PERSONALITY.get(key[2:], {}))
                    if ok:
                        _p.set_setting(key, val)
                    send(conn, {"type": "ok" if ok else "error"})
                elif kind == "get_profile":
                    from ..memory import profile as _p
                    send(conn, {"type": "profile", "facts": [
                        {"id": f["id"], "category": _p.CATEGORIES.get(f["category"], f["category"]),
                         "item": f["item"], "polarity": f["polarity"], "detail": f["detail"],
                         "source": f["source"]} for f in _p.facts(min_conf=0.45)],
                        "routines": _p.routines(),
                        "learning": _p._setting("learning", "on")})
                elif kind == "forget_fact":
                    from ..memory import profile as _p
                    if msg.get("all"):
                        n = _p.forget(everything=True)
                    else:
                        n = int(_p.forget_id(int(msg.get("id", 0))))
                    _p.invalidate()
                    send(conn, {"type": "ok", "forgotten": n})
                elif kind == "suggestion_feedback":
                    from .. import proactive
                    proactive.feedback(str(msg.get("id", "")), str(msg.get("outcome", "")))
                    send(conn, {"type": "ok"})
                elif kind == "run":
                    self._run(conn, reader, msg)
                elif kind == "listen":
                    self._listen(conn, reader, msg)
                elif kind == "stop_listening":
                    self._stop_listening.set()
                    send(conn, {"type": "listening_stopped"})
                else:
                    send(conn, {"type": "error", "text": f"unknown request {kind!r}"})
        except Exception:
            log.exception("connection error")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _input_status() -> str:
        """Checked here, in the daemon, because the permission is per launcher."""
        try:
            from ..tools.control import input_allowed, launcher_app
            if input_allowed():
                return "allowed"
            return f"BLOCKED - enable Accessibility for {launcher_app()}"
        except Exception as e:
            return f"unknown ({e})"

    def _switch_backend(self, name: str) -> dict:
        """
        Swap the reasoning backend without restarting the engine.

        The conversation is kept: `messages` is backend-neutral, so a turn that
        started on Claude can be finished by the local model and vice versa.
        Switching *to* local pays the model load once, and is refused while a
        request is in flight rather than swapping the model under it.
        """
        want = resolve_kind(name)
        if not want:
            return {"type": "error",
                    "text": f"unknown backend {name!r} — claude, local or cloud"}
        if want == getattr(self.backend, "_kind", "local"):
            return {"type": "backend_ok", "backend": backend_name(self.backend),
                    "kind": want, "text": "already active"}
        if self._busy.locked():
            return {"type": "error", "text": "busy — /stop the current request first"}

        import jarvis.core.llm as _llm
        prev, prev_msgs = self.backend, list(self.agent.messages)
        try:
            _llm._BACKEND = None
            b = get_backend(want)
            if getattr(b, "_kind", "") != want:
                _llm._BACKEND = prev
                return {"type": "error",
                        "text": f"{want} backend unavailable (no API key?)"}
            self.status = f"switching to {want}"
            b.load()
            b.warm()
            self.backend = b
            self.agent.llm = b
            self.agent.messages = prev_msgs
            self.status = "ready"
            if is_remote(b) and not is_remote(prev):
                # 14 GB of weights nothing is going to read again
                try:
                    prev.unload()
                except Exception as e:
                    log.warning("could not unload the local model: %s", e)
            if not is_remote(b) and not getattr(self, "_warm_thread", False):
                self._warm_thread = True
                threading.Thread(target=self._keep_warm, daemon=True).start()
            log.info("backend switched to %s", backend_name(b))
            return {"type": "backend_ok", "backend": backend_name(b), "kind": want}
        except Exception as e:
            _llm._BACKEND = prev
            self.backend, self.agent.llm, self.status = prev, prev, "ready"
            log.exception("backend switch failed")
            return {"type": "error", "text": f"switch failed: {e}"}

    def _listen(self, conn: socket.socket, reader: Reader, msg: dict) -> None:
        """
        Hold-to-talk driven by the companion app.

        The daemon owns the microphone because it already owns Whisper and the
        VAD; the app just says when to start and stop. Recording ends on either
        the key release or a natural pause, whichever comes first, so a released
        key never leaves the mic open.
        """
        import numpy as np

        if not CONFIG.voice.enabled:
            send(conn, {"type": "error",
                        "text": "voice is turned off for now - set "
                                "JARVIS_VOICE_ENABLED=1 and restart to use it"})
            return
        if not self._listen_lock.acquire(blocking=False):
            send(conn, {"type": "error",
                        "text": "already listening — release and try again"})
            return
        self._stop_listening.clear()
        try:
            from ..voice.listener import MIC, capture_while_held
            from ..voice.stt import WhisperSTT
        except Exception as e:
            send(conn, {"type": "error", "text": f"voice unavailable: {e}"})
            self._listen_lock.release()
            return

        send(conn, {"type": "status", "text": "listening"})
        try:
            # Push-to-talk: record exactly while the key is down. Waiting for a
            # pause would add the VAD silence window to every utterance.
            utt = capture_while_held(MIC, stop_event=self._stop_listening)
        except Exception as e:
            send(conn, {"type": "error", "text": f"microphone error: {e}"})
            self._listen_lock.release()
            return

        if utt is None or getattr(utt, "audio", None) is None or not len(utt.audio):
            send(conn, {"type": "transcript", "text": ""})
            send(conn, {"type": "done", "text": "", "meta": {}})
            self._listen_lock.release()
            return

        send(conn, {"type": "status", "text": "transcribing"})
        try:
            stt = self._stt or WhisperSTT()
            self._stt = stt
            text = stt.transcribe(utt.audio, 16000).strip()
        except Exception as e:
            send(conn, {"type": "error", "text": f"transcription failed: {e}"})
            self._listen_lock.release()
            return
        send(conn, {"type": "transcript", "text": text})
        self._listen_lock.release()
        if not text:
            send(conn, {"type": "done", "text": "", "meta": {}})
            return
        self._run(conn, reader, {"text": text, "voice": bool(msg.get("voice", True))})

    def _run(self, conn: socket.socket, reader: Reader, msg: dict) -> None:
        if not self.ready.is_set():
            send(conn, {"type": "status", "text": self.status})
            self.ready.wait(timeout=300)
        if not self.ready.is_set() or self.status.startswith("failed"):
            send(conn, {"type": "error", "text": (
                "the engine could not start: " + self.status.splitlines()[-1][:300]
                if self.status.startswith("failed") else
                f"the engine is still starting ({self.status}) - try again in a moment")
                + f" (log: {LOG_DIR / 'daemon.log'})"})
            return
        # Tell the keep-warm thread to get out of the way, then wait briefly:
        # a background pass yields in milliseconds, so rejecting instantly would
        # turn a non-issue into a visible failure.
        self._request_pending.set()
        got = self._busy.acquire(timeout=1.5)
        if not got:
            # A request whose app has gone (quit, crashed, restarted) is
            # abandoned in favour of this one: the person who asked for it is
            # no longer waiting, and the new request must not get "busy".
            cur = self._turn
            if cur and (cur.get("cancel").is_set() or _peer_gone(cur.get("conn"))):
                self._cancel_turn("its app disconnected; a new request arrived")
            got = self._busy.acquire(timeout=8.0)
        if not got:
            self._request_pending.clear()
            send(conn, {"type": "error",
                        "text": "still working on the previous request — "
                                "press stop to cancel it"})
            return
        self._turn_seq += 1
        turn = {"id": self._turn_seq, "cancel": threading.Event(),
                "started": time.time(), "conn": conn, "why": ""}
        self._turn = turn
        # Held while a confirmation prompt reads this connection. The prompt
        # runs on the turn's worker thread; once the request ends, the
        # connection loop reads again, and the two must never read at once -
        # a prompt still waiting would take the client's next request as its
        # answer, and that request would never be answered.
        reading = threading.Lock()
        # Clients read until "done" or "error" and then stop; anything sent
        # after one of those is read as the reply to their *next* request.
        # So exactly one of them ends every request.
        ended = False
        try:
            # Route confirmation prompts back to whichever client is connected.
            def confirm(name, args, verdict) -> bool:
                # A stopped request finishing in the background asks nobody.
                if CURRENT_TURN.get() != turn["id"] or turn["cancel"].is_set():
                    return False
                with reading:
                    if not send(conn, {"type": "confirm", "tool": name, "args": args,
                                       "reason": verdict.reason, "risk": verdict.risk.name,
                                       "category": verdict.category}):
                        return False
                    deadline = time.time() + 180
                    while time.time() < deadline:
                        # Short reads, so a stop is noticed while the prompt
                        # is still on screen (it used to wait out 180 s).
                        if turn["cancel"].is_set():
                            return False
                        reply = reader.next_message(timeout=0.5)
                        if reply is None:
                            if _peer_gone(conn):
                                return False
                            continue
                        rtype = reply.get("type")
                        if rtype == "interrupt":
                            self._cancel_turn("stopped by the user")
                            return False
                        if rtype not in (None, "confirm_reply"):
                            # Not an answer: leave it for the connection loop.
                            reader.push_back(reply)
                            return False
                        if reply.get("remember"):
                            REGISTRY.granted.add(verdict.category)
                        return bool(reply.get("approve"))
                    return False

            REGISTRY.confirm_hook = confirm
            text = msg.get("text", "")
            voice = bool(msg.get("voice"))
            if msg.get("fresh"):
                self.agent.reset()
            agent = self.agent
            before = list(agent.messages)
            events: queue.Queue = queue.Queue()

            def work() -> None:
                CURRENT_TURN.set(turn["id"])
                stream = agent.run(text, voice=voice)
                try:
                    for ev in stream:
                        events.put(ev)
                except Exception as e:                      # noqa: BLE001
                    log.exception("run failed")
                    events.put(e)
                finally:
                    stream.close()
                    events.put(None)

            threading.Thread(target=work, daemon=True, name=f"turn-{turn['id']}").start()
            while True:
                try:
                    ev = events.get(timeout=0.25)
                except queue.Empty:
                    if ended:
                        # Already answered; the worker is only wrapping up. A
                        # client hanging up now must not rewrite the finished
                        # turn as "stopped".
                        if turn["cancel"].is_set():
                            break
                        continue
                    if not turn["cancel"].is_set() and _peer_gone(conn):
                        turn["cancel"].set()
                        turn["why"] = "the app disconnected"
                    if turn["cancel"].is_set():
                        self._abandon(turn, agent, before, text)
                        send(conn, {"type": "done", "text": "Stopped.",
                                    "meta": {"stopped": True}})
                        ended = True
                        break
                    continue
                if ev is None:
                    break
                if isinstance(ev, Exception):
                    if not ended:
                        send(conn, {"type": "error", "text": f"{type(ev).__name__}: {ev}"})
                    ended = True
                    continue
                if ended:
                    continue
                if turn["cancel"].is_set():
                    self._abandon(turn, agent, before, text)
                    send(conn, {"type": "done", "text": "Stopped.",
                                "meta": {"stopped": True}})
                    ended = True
                    break
                payload = {
                    "type": ev.type, "text": ev.text, "tool": ev.tool,
                    "args": ev.args, "ok": ev.ok, "meta": ev.meta,
                    "display": getattr(ev.result, "display", "")
                               if ev.result else ""}
                # Tools that draw on screen put their payload in
                # result.meta["ui"]. Only that key is forwarded: the full
                # result can be large and the companion has no use for it.
                ui = (getattr(ev.result, "meta", None) or {}).get("ui") \
                    if ev.result is not None else None
                if ui:
                    payload["ui"] = ui
                ok = send(conn, payload)
                if ev.type in ("done", "error"):
                    # Finished: a client that hangs up now has its answer, and
                    # the turn must stay in the history rather than be
                    # rewritten as "stopped".
                    ended = True
                    continue
                if not ok:
                    # the client hung up; stop the work rather than finish
                    # talking to nobody
                    turn["cancel"].set()
                    turn["why"] = "the app disconnected"
                    self._abandon(turn, agent, before, text)
                    ended = True
                    break
            if not ended:
                # The work ended without saying so; never leave a client
                # waiting for an end that is not coming.
                send(conn, {"type": "done", "text": "", "meta": {}})
                ended = True
        except Exception as e:
            log.exception("run failed")
            if not ended:
                send(conn, {"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            if self._turn is turn:
                self._turn = None
            REGISTRY.confirm_hook = None
            # A confirmation prompt still reading this connection lets go
            # within half a second of the stop; wait for it (bounded) before
            # the connection loop reads the next request.
            if reading.acquire(timeout=2.0):
                reading.release()
            self._request_pending.clear()
            self._busy.release()

    # -- stopping ------------------------------------------------------------
    def _cancel_turn(self, why: str) -> None:
        """Ask the request in progress to stop. Its own handler notices within
        a quarter of a second and frees the engine, even when a tool is still
        stuck: that tool is left to finish on a detached copy of the agent."""
        cur = self._turn
        self.agent.interrupt()
        if cur:
            cur["why"] = cur.get("why") or why
            cur["cancel"].set()

    def _reset_conversation(self) -> None:
        """Forget the conversation. A request still running is stopped first:
        clearing the history under it left its tool results with no call in
        front of them, and its stop then put the old conversation back."""
        if self._busy.locked():
            self._cancel_turn("the conversation was reset")
        got = self._busy.acquire(timeout=5.0)
        try:
            self.agent.reset()
            self.backend.warm()
        finally:
            if got:
                self._busy.release()

    def _abandon(self, turn: dict, agent: Agent, before: list, text: str) -> None:
        """Detach a stopped request so the next one starts clean right now.

        The old Agent object keeps running on its own thread until its current
        tool returns (a blocked call cannot be interrupted from outside), then
        sees its stop flag and ends. It keeps its own message list, so nothing
        it does afterwards can leak into the new conversation, and its tool
        calls are refused confirmation by turn id.
        """
        agent.interrupt()
        killed = _kill_children_since(turn["started"])
        done = [l.strip() for l in getattr(agent, "_trace", [])[-4:]]
        note = "[Stopped by the user before finishing"
        if done:
            note += ". Already done: " + "; ".join(done)
        note += ".]"
        fresh = Agent(self.backend, session_id=agent.session_id)
        fresh.messages = before + [{"role": "user", "content": text},
                                   {"role": "assistant", "content": note}]
        fresh._turn_index = agent._turn_index
        fresh.follow_ups = agent.follow_ups
        fresh._last_followup_turn = agent._last_followup_turn
        if self.agent is agent:
            self.agent = fresh
        log.info("request %s stopped (%s) after %.1fs; ended %d helper process(es)",
                 turn["id"], turn.get("why") or "stop", time.time() - turn["started"], killed)


def _peer_gone(conn) -> bool:
    """True when the client on the other end has closed its socket."""
    if conn is None:
        return True
    try:
        data = conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        return data == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


def _kill_children_since(started: float) -> int:
    """End processes this engine launched for a request that was stopped
    (osascript, headless Chrome, Blender, shell commands) - anything started
    after the request began. Long-lived helpers started earlier are kept."""
    import subprocess as _sp
    try:
        out = _sp.run(["ps", "-axo", "pid=,ppid=,etime="], capture_output=True,
                      text=True, timeout=5).stdout
    except Exception:
        return 0
    rows = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), _etime_s(parts[2]))
    me = os.getpid()
    age_limit = time.time() - started + 1.0
    tree, frontier = set(), {me}
    while frontier:
        nxt = {p for p, (pp, _) in rows.items() if pp in frontier and p not in tree}
        tree |= nxt
        frontier = nxt
    victims = [p for p in tree if rows[p][1] <= age_limit]
    for p in victims:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    return len(victims)


def _etime_s(e: str) -> float:
    """ps elapsed time ([[dd-]hh:]mm:ss) in seconds."""
    days = 0
    if "-" in e:
        d, e = e.split("-", 1)
        days = int(d)
    parts = [int(x) for x in e.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


def main() -> None:
    Daemon().serve()


if __name__ == "__main__":
    main()
