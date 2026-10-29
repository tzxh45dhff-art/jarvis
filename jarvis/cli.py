"""JARVIS command line: interactive shell, one-shot tasks, voice mode."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from .config import CONFIG, LOG_DIR, SOCKET_PATH
from .daemon.client import Client, DaemonError, daemon_pid
from .ui import console as ui


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _connect(quiet: bool = False) -> Client:
    c = Client()
    first = [True]

    def status(msg: str) -> None:
        if not quiet and first[0]:
            ui.info(msg)
            first[0] = False

    if not SOCKET_PATH.exists() and not quiet:
        ui.info("starting engine (loading the model takes about 20 seconds "
                "the first time)…")
    try:
        c.connect(on_status=status)
    except DaemonError as e:
        ui.error(str(e))
        sys.exit(1)
    # An engine started before the code changed is still running the old code:
    # fixes sit unused until someone remembers `jarvis restart`. Restart it here,
    # from this process, so the new engine inherits this terminal's macOS
    # permissions (Accessibility, Calendars...) exactly as a manual restart would.
    st = c.ping() or {}
    from . import code_stamp
    # (an engine too old to report its version at all is, by definition, stale)
    if st and st.get("code") != code_stamp():
        if st.get("busy"):
            if not quiet:
                ui.warn("JARVIS has new code, but the engine is busy - it will "
                        "update next time")
        else:
            if not quiet:
                ui.info("JARVIS was updated - restarting the engine…")
            c.shutdown()
            c.close()
            for _ in range(50):
                if not SOCKET_PATH.exists():
                    break
                time.sleep(0.2)
            time.sleep(0.5)
            c = Client()
            try:
                c.connect(on_status=status)
            except DaemonError as e:
                ui.error(str(e))
                sys.exit(1)
            st = c.ping() or {}
    # wait for the model to finish loading, showing progress
    if not st.get("ready"):
        last = ""
        with ui.console.status("[dim]starting…[/dim]", spinner="dots") as sp:
            for _ in range(600):
                st = c.ping() or {}
                s = st.get("status", "")
                if s != last:
                    sp.update(f"[dim]{s}…[/dim]")
                    last = s
                if st.get("ready"):
                    break
                if s.startswith("failed"):
                    ui.error(s)
                    sys.exit(1)
                time.sleep(0.4)
    return c


# --------------------------------------------------------------------------- #
# interactive shell
# --------------------------------------------------------------------------- #
class Shell:
    def __init__(self, client: Client, speak: bool = False):
        self.client = client
        self.speak = speak
        self.speaker = None
        self.show_thinking = CONFIG.ui.show_thinking
        self._voice_stop = threading.Event()

    # -- voice plumbing ----------------------------------------------------
    def _ensure_speaker(self):
        if self.speaker is None:
            from .voice.tts import Speaker
            self.speaker = Speaker()
            with ui.console.status("[dim]loading speech…[/dim]"):
                self.speaker.warm()
        return self.speaker

    def _listen_once(self, wake: bool = False) -> str | None:
        from .voice.listener import MIC, capture_utterance, wait_for_wake
        from .voice.stt import WhisperSTT
        pre = None
        if wake:
            ui.info('say "Hey JARVIS"  (ctrl-c to stop)')
            pre = wait_for_wake(MIC, stop_event=self._voice_stop)
            if pre is None:
                return None
            ui.ok("wake word detected")
            if self.speaker:
                self.speaker.stop()
        with ui.console.status("[dim]listening…[/dim]", spinner="dots"):
            utt = capture_utterance(MIC, preroll=pre)
        if utt is None:
            ui.info("heard nothing")
            return None
        with ui.console.status("[dim]transcribing…[/dim]", spinner="dots"):
            text = WhisperSTT().transcribe(utt.audio, 16000)
        return text.strip() or None

    def voice_mode(self) -> None:
        self._ensure_speaker()
        self._voice_stop.clear()
        ui.rule("voice mode")
        ui.info('wake word: "Hey JARVIS"   ·   ctrl-c to return to text')
        try:
            while not self._voice_stop.is_set():
                text = self._listen_once(wake=True)
                if not text:
                    continue
                ui.user_echo(text)
                if text.lower().strip(" .!?") in ("stop", "exit", "quit", "text mode",
                                                  "never mind", "nevermind"):
                    break
                self._dispatch(text, speak=True)
        except KeyboardInterrupt:
            ui.console.print()
        finally:
            self._voice_stop.set()
            if self.speaker:
                self.speaker.stop()
            ui.rule("text mode")

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, text: str, speak: bool = False) -> None:
        r = ui.TurnRenderer(show_thinking=self.show_thinking)
        interrupted = threading.Event()

        def on_sigint(*_):
            interrupted.set()
            self.client.interrupt()
            if self.speaker:
                self.speaker.stop()

        old = signal.signal(signal.SIGINT, on_sigint)
        try:
            for ev in self.client.run(text, voice=speak,
                                      on_confirm=ui.confirm_prompt):
                r.handle(ev)
        except (OSError, DaemonError) as e:
            # the next request reconnects (and restarts the engine if needed)
            self.client.close()
            ui.error(f"lost connection to the engine ({e}) - "
                     f"see {LOG_DIR / 'daemon.log'}")
        finally:
            signal.signal(signal.SIGINT, old)
            r.close()
        if interrupted.is_set():
            ui.warn("interrupted")
        if speak and r.text:
            self._ensure_speaker().say(r.text)

    # -- commands ----------------------------------------------------------
    def command(self, line: str) -> bool:
        """Handle a /command. Returns False to exit the shell."""
        cmd, _, arg = line[1:].partition(" ")
        cmd, arg = cmd.strip().lower(), arg.strip()
        if cmd in ("q", "quit", "exit"):
            return False
        if cmd == "kill":
            self.client.shutdown()
            ui.info("engine stopped")
            return False
        if cmd in ("h", "help", "?"):
            ui.help_panel()
        elif cmd in ("voice", "listen", "mute", "unmute") and not CONFIG.voice.enabled:
            ui.warn("voice is turned off for now (it frees ~1-2 GB). Turn it on "
                    "with JARVIS_VOICE_ENABLED=1")
        elif cmd == "voice":
            self.voice_mode()
        elif cmd == "listen":
            t = self._listen_once(wake=False)
            if t:
                ui.user_echo(t)
                self._dispatch(t, speak=True)
        elif cmd == "mute":
            self._ensure_speaker().muted = True
            ui.info("speech output off")
        elif cmd == "unmute":
            self._ensure_speaker().muted = False
            ui.info("speech output on")
        elif cmd == "new":
            self.client.reset()
            ui.info("fresh conversation")
        elif cmd == "status":
            st = self.client.status() or {}
            st["pid"] = daemon_pid()
            st["logs"] = str(LOG_DIR)
            ui.show_status(st)
        elif cmd in ("backend", "model"):
            if not arg:
                st = self.client.status() or {}
                ui.info(f"backend: {st.get('backend', '?')}  "
                        f"({st.get('kind', '?')})")
                u = st.get("usage") or {}
                if u:
                    ui.info(f"tokens this session: in={u.get('in', 0)} "
                            f"out={u.get('out', 0)} "
                            f"cached={u.get('cache_read', 0)}")
                ui.info("switch with /backend claude | local | cloud")
            else:
                with ui.console.status(f"[dim]switching to {arg}…[/dim]"):
                    r = self.client.set_backend(arg) or {}
                if r.get("type") == "backend_ok":
                    ui.ok(f"backend: {r.get('backend')} {r.get('text', '')}".strip())
                else:
                    ui.warn(r.get("text", "switch failed"))
        elif cmd == "think":
            self.show_thinking = not self.show_thinking
            ui.info(f"reasoning trace {'shown' if self.show_thinking else 'hidden'}")
        elif cmd == "stop":
            self.client.interrupt()
            ui.info("interrupt sent")
        elif cmd in ("auto", "autonomous"):
            # Long unattended tasks should not stall on a prompt. This grants
            # the reversible categories for the session; sending, deleting,
            # system changes and shortcuts still ask. The grant goes to the
            # engine: it is the engine's registry that asks, and changing this
            # process's copy (as this once did) changed nothing.
            off = arg in ("off", "0", "no")
            try:
                r = self.client.grant(
                    [] if off else ["control", "ui_input", "generic", "overwrite",
                                    "install", "system_pref"], clear=off) or {}
            except (OSError, DaemonError) as e:
                r = {"type": "error", "text": str(e)}
            if r.get("type") != "ok":
                ui.warn("the engine did not accept that (it may need `jarvis "
                        "restart` to pick up this version)")
            elif off:
                ui.info("autonomy off — normal confirmations restored")
            else:
                ui.ok("autonomy on for this session — JARVIS will not stop for "
                      "reversible actions")
                ui.info("still gated: sending messages, deleting, shortcuts")
        elif cmd == "tools":
            from .tools import REGISTRY
            names = REGISTRY.names()
            ui.console.print("  " + "  ".join(names), style=CONFIG.ui.dim)
            ui.info(f"{len(names)} tools")
        elif cmd == "memory":
            self._dispatch("List everything you remember about me, grouped by kind. "
                           "Use the list_memories tool.")
        elif cmd == "forget":
            if not arg:
                ui.warn("usage: /forget <what to forget>")
            else:
                self._dispatch(f"Forget this from memory: {arg}")
        elif cmd == "undo":
            from .safety import journal
            ok, msg = journal.undo()
            (ui.ok if ok else ui.warn)(msg)
        elif cmd == "history":
            from .safety import journal
            rows = journal.recent(15)
            if not rows:
                ui.info("no file changes recorded")
            for m in rows:
                ui.console.print(
                    f"  [dim]{time.strftime('%H:%M', time.localtime(m.ts))}[/dim] "
                    f"[{CONFIG.ui.accent}]{m.op:<9}[/] {m.path}")
        else:
            ui.warn(f"unknown command /{cmd} — try /help")
        return True

    # -- loop --------------------------------------------------------------
    def run(self) -> None:
        st = self.client.status() or {}
        ui.banner(f"{st.get('tools', '?')} tools · "
                  f"{st.get('backend', 'gemma-4 26B local')}")
        ui.info("type /help for commands, or just start talking")
        ui.console.print()
        if self.speak:
            self.voice_mode()
        while True:
            try:
                line = ui.console.input(
                    f"[bold {CONFIG.ui.user_color}]❯[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                ui.console.print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if not self.command(line):
                    break
                continue
            self._dispatch(line, speak=bool(self.speaker and not self.speaker.muted
                                            and self.speak))
        ui.info("engine still running in the background — `jarvis stop` to shut it down")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="jarvis", description="Local AI computer assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  jarvis                              interactive shell
  jarvis "organise my Downloads"      one-shot task
  jarvis --voice                      start in voice mode
  jarvis --speak "what's on my screen?"
  jarvis status | stop | restart      manage the background engine
  jarvis companion start|stop|enable  on-screen cursor (enable = start at login)
""")
    ap.add_argument("task", nargs="*", help="run one task and exit")
    ap.add_argument("--voice", "-v", action="store_true", help="voice mode")
    ap.add_argument("--speak", "-s", action="store_true", help="speak the answer")
    ap.add_argument("--think", action="store_true", help="show the reasoning trace")
    ap.add_argument("--claude", action="store_true",
                    help="use the Claude API (default when a key is configured)")
    ap.add_argument("--local", action="store_true",
                    help="use the local Gemma model instead of the API")
    ap.add_argument("--cloud", action="store_true",
                    help="use the Gemini API")
    ap.add_argument("--fresh", action="store_true", help="ignore prior conversation")
    ap.add_argument("--no-daemon", action="store_true",
                    help="run in this process instead of the background engine")
    args = ap.parse_args(argv)
    for flag in ("claude", "local", "cloud"):
        if getattr(args, flag, False):
            os.environ["JARVIS_BACKEND"] = flag
            break

    task = " ".join(args.task).strip()

    # sub-commands
    if task.startswith("companion"):
        return _companion(task.split()[1] if len(task.split()) > 1 else "toggle")
    if task in ("status", "stop", "restart", "start", "doctor", "logs"):
        return _manage(task)
    if task.split()[:1] == ["cost"]:
        return _cost(task.split()[1:])

    if args.no_daemon:
        return _inproc(task, args)

    client = _connect(quiet=bool(task))
    try:
        # An engine already running may be on a different backend than the flag
        # asks for, and the flag only reaches a daemon this process spawned.
        want = os.environ.get("JARVIS_BACKEND", "")
        if want:
            st = client.status() or {}
            from .core.llm import resolve_kind
            if resolve_kind(want) != st.get("kind"):
                r = client.set_backend(want) or {}
                if r.get("type") != "backend_ok" and not task:
                    ui.warn(r.get("text", "could not switch backend"))
        if task:
            r = ui.TurnRenderer(show_thinking=args.think)
            failed = False
            try:
                for ev in client.run(task, voice=args.speak, fresh=args.fresh,
                                     on_confirm=ui.confirm_prompt):
                    failed = failed or ev.get("type") == "error"
                    r.handle(ev)
            except (OSError, DaemonError) as e:
                failed = True
                ui.error(f"lost connection to the engine ({e}) - "
                         f"see {LOG_DIR / 'daemon.log'}")
            finally:
                r.close()
            if args.speak and r.text and CONFIG.voice.enabled:
                from .voice.tts import Speaker
                sp = Speaker()
                sp.say(r.text, block=True)
            return 1 if failed else 0
        Shell(client, speak=args.voice and CONFIG.voice.enabled).run()
        return 0
    finally:
        client.close()


def _companion(action: str) -> int:
    """
    Start/stop the on-screen companion.

    Its menu bar item can be invisible when the menu bar is full — macOS creates
    it but declines to draw it — so it must be controllable from here too.
    """
    import subprocess
    app = "/Applications/JARVIS Companion.app"
    running = subprocess.run(["pgrep", "-f", "JARVIS Companion"],
                             capture_output=True).returncode == 0
    plist = os.path.expanduser(
        "~/Library/LaunchAgents/com.example.jarvis.companion.plist")
    if action in ("enable", "autostart", "login"):
        subprocess.run(["launchctl", "unload", plist], capture_output=True)
        r = subprocess.run(["launchctl", "load", "-w", plist], capture_output=True)
        if r.returncode == 0:
            ui.ok("companion will now start at login and restart if it crashes")
        else:
            ui.error((r.stderr or b"").decode()[:150])
        return 0
    if action in ("disable", "noautostart"):
        subprocess.run(["launchctl", "unload", "-w", plist], capture_output=True)
        ui.ok("companion will no longer start at login")
        return 0
    if action in ("stop", "quit", "kill"):
        subprocess.run(["launchctl", "unload", plist], capture_output=True)
        subprocess.run(["pkill", "-f", "JARVIS Companion"], capture_output=True)
        ui.ok("companion stopped")
        return 0
    if action == "status":
        import json
        state_path = os.path.expanduser("~/.jarvis/run/companion.json")
        ui.info(f"companion is {'running' if running else 'not running'}")
        try:
            st = json.load(open(state_path))
            fresh = (time.time() - st.get("updated", 0)) < 20
            ui.console.print(
                f"  hotkey       : [bold]{st.get('hotkey')}[/bold]")
            ui.console.print(
                f"  accessibility: {'granted' if st.get('accessibility') else 'NOT granted'}"
                f"{'' if st.get('accessibility') else '  (hold-to-talk needs it)'}")
            ui.console.print(f"  engine       : {'up' if st.get('engine') else 'down'}")
            if not fresh:
                ui.warn("state file is stale — the app may not be running")
        except Exception:
            pass
        return 0
    if action in ("fix", "permission", "permissions"):
        # The usual cause of "it does nothing" is a stale Accessibility entry
        # left by an earlier, differently-signed build: macOS lists it as
        # granted but does not honour it. Clearing it forces a fresh prompt.
        subprocess.run(["tccutil", "reset", "Accessibility",
                        "com.example.jarvis.companion"], capture_output=True)
        subprocess.run(["pkill", "-f", "JARVIS Companion"], capture_output=True)
        time.sleep(1)
        subprocess.run(["open", app], capture_output=True)
        subprocess.run(["open", "x-apple.systempreferences:"
                        "com.apple.preference.security?Privacy_Accessibility"],
                       capture_output=True)
        ui.ok("cleared the old permission and reopened the app")
        ui.info("in the pane that just opened, switch ON 'JARVIS Companion'")
        ui.info("(⌃⌥Space works right now without it)")
        return 0
    if action == "toggle" and running:
        subprocess.run(["pkill", "-f", "JARVIS Companion"], capture_output=True)
        ui.ok("companion stopped")
        return 0
    if not os.path.exists(app):
        ui.error(f"{app} not found — build it with companion/build.sh")
        return 1
    subprocess.run(["open", app], capture_output=True)
    ui.ok("companion started — hold ⌃⌥ to talk")
    return 0


def _manage(cmd: str) -> int:
    if cmd == "status":
        c = Client(autostart=False)
        try:
            c.connect(timeout=3)
            st = c.status() or {}
            st["pid"] = daemon_pid()
            st["socket"] = str(SOCKET_PATH)
            ui.show_status(st)
        except DaemonError:
            ui.info("engine is not running")
        return 0
    if cmd == "logs":
        ui.info(str(LOG_DIR / "daemon.log"))
        os.system(f"tail -n 60 {LOG_DIR / 'daemon.log'}")
        return 0
    if cmd in ("stop", "restart"):
        c = Client(autostart=False)
        try:
            c.connect(timeout=3)
            c.shutdown()
            ui.ok("engine stopped")
        except DaemonError:
            ui.info("engine was not running")
        time.sleep(1.0)
        if cmd == "stop":
            return 0
    if cmd in ("start", "restart"):
        _connect()
        ui.ok("engine ready")
    if cmd == "doctor":
        return _doctor()
    return 0


def _cost(argv: list[str]) -> int:
    """`jarvis cost [days]` - what the models cost, from the request ledger."""
    from .core import ledger
    try:
        days = float(argv[0]) if argv else 1.0
    except ValueError:
        days = 1.0
    s = ledger.summary(days)
    span = "today (24 h)" if days == 1 else f"the last {days:g} days"
    print(f"Model cost for {span}: ₹{s['inr']:.2f} over {s['requests']} requests")
    if s["requests"]:
        print(f"  prompt cache: {s['cache_hit'] * 100:.0f}% of input tokens were cache hits; "
              f"{s['write']:,} tokens written to the cache")
        for title, d in (("by model", s["by_model"]), ("by kind", s["by_kind"])):
            print(f"  {title}: " + ", ".join(f"{k} ₹{v['inr']:.2f} ({v['requests']})"
                                            for k, v in sorted(d.items(), key=lambda kv: -kv[1]["inr"])))
    return 0


def _doctor() -> int:
    from .diagnostics import run_doctor
    return run_doctor()


def _engine_running() -> bool:
    from .daemon.server import LOCK_FILE
    import fcntl
    if not LOCK_FILE.exists():
        return False
    try:
        fh = open(LOCK_FILE, "r+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
        return False              # we got the lock, so nothing holds it
    except OSError:
        return True


def _inproc(task: str, args) -> int:
    """
    Run without the daemon (useful for debugging).

    Refuses while the engine is up: the model is 14 GB, and a second copy on a
    24 GB machine pushes it into swap.
    """
    if _engine_running():
        ui.error("the background engine is already running with the model loaded.")
        ui.info("running a second copy would need another 14 GB of memory.")
        ui.info("use `jarvis \"...\"` to talk to the running engine, "
                "or `jarvis stop` first.")
        return 2
    from .core.agent import Agent
    from .core.llm import get_backend
    from .tools import REGISTRY
    b = get_backend()
    with ui.console.status("[dim]loading model…[/dim]"):
        b.load()
        b.warm()
    REGISTRY.confirm_hook = lambda n, a, v: ui.confirm_prompt(
        {"tool": n, "args": a, "reason": v.reason, "risk": v.risk.name,
         "category": v.category})[0]
    ag = Agent(b)
    if not task:
        ui.banner("in-process mode")
        while True:
            try:
                task = ui.console.input("[bold]❯[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not task or task in ("/quit", "/exit"):
                break
            r = ui.TurnRenderer(show_thinking=args.think)
            for ev in ag.run(task):
                r.handle({"type": ev.type, "text": ev.text, "tool": ev.tool,
                          "args": ev.args, "ok": ev.ok, "meta": ev.meta})
            r.close()
        return 0
    r = ui.TurnRenderer(show_thinking=args.think)
    for ev in ag.run(task):
        r.handle({"type": ev.type, "text": ev.text, "tool": ev.tool,
                  "args": ev.args, "ok": ev.ok, "meta": ev.meta})
    r.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
