"""
Terminal interface.

Shows what JARVIS is doing without flooding the screen: streaming prose, one
dim line per tool call, and a compact stats footer. Full detail goes to the log.
"""
from __future__ import annotations

import sys
import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from ..config import CONFIG

C = CONFIG.ui
console = Console(highlight=False)

# The spinner is a Rich `Live`, which owns the terminal while it runs. Reading
# input underneath one silently swallows the keystrokes: the user types "y",
# nothing echoes, and Enter returns "" — which read as a refusal. Anything that
# needs stdin has to suspend it first, so the active Live is tracked here.
_ACTIVE_LIVE: "Live | None" = None


def suspend_live():
    """Stop the running spinner (if any) and return a token to restart it."""
    global _ACTIVE_LIVE
    live, _ACTIVE_LIVE = _ACTIVE_LIVE, None
    if live is not None:
        try:
            live.stop()
        except Exception:
            pass
    return live is not None

BANNER = r"""
     ██  █████  ██████  ██    ██ ██ ███████
     ██ ██   ██ ██   ██ ██    ██ ██ ██
     ██ ███████ ██████  ██    ██ ██ ███████
██   ██ ██   ██ ██   ██  ██  ██  ██      ██
 █████  ██   ██ ██   ██   ████   ██ ███████
"""

_TOOL_ICON = {
    "read": "◦", "search": "⌕", "write_new": "+", "overwrite": "±",
    "delete": "⌫", "control": "⌘", "compute": "=", "generic": "›",
    "system": "⚙", "install": "⇩", "network_write": "↑",
}


def banner(subtitle: str = "") -> None:
    console.print(Text(BANNER, style=f"bold {C.accent}"))
    line = "  local · private · on-device"
    if subtitle:
        line += f"   —   {subtitle}"
    console.print(Text(line, style=C.dim))
    console.print()


def rule(text: str = "") -> None:
    console.print(Rule(text, style=C.dim))


def user_echo(text: str) -> None:
    console.print(Text("❯ ", style=f"bold {C.user_color}") + Text(text, style="bold white"))


def info(text: str) -> None:
    console.print(Text("  " + text, style=C.dim))


def warn(text: str) -> None:
    console.print(Text("  ! " + text, style=C.warn))


def error(text: str) -> None:
    console.print(Text("  ✗ " + text, style=C.err))


def ok(text: str) -> None:
    console.print(Text("  ✓ " + text, style=C.ok))


def _fmt_args(args: dict, width: int = 68) -> str:
    if not args:
        return ""
    bits = []
    for k, v in args.items():
        s = str(v).replace("\n", "⏎")
        if len(s) > 46:
            s = s[:43] + "…"
        bits.append(f"{k}={s}")
    out = " ".join(bits)
    return out if len(out) <= width else out[: width - 1] + "…"


class TurnRenderer:
    """Renders one request: status line, tool trace, streamed answer."""

    def __init__(self, show_thinking: bool = False):
        self.show_thinking = show_thinking
        self.text_started = False
        self.buffer: list[str] = []
        self.t0 = time.time()
        self._live: Live | None = None
        self._spinner_label = "thinking"
        self._pending_tools: dict[str, float] = {}

    # -- spinner -----------------------------------------------------------
    def _start_spin(self, label: str) -> None:
        self._spinner_label = label
        global _ACTIVE_LIVE
        if self._live is None:
            self._live = Live(self._spin_renderable(), console=console,
                              refresh_per_second=12, transient=True)
            self._live.start()
            _ACTIVE_LIVE = self._live
        else:
            self._live.update(self._spin_renderable())

    def _spin_renderable(self):
        return Text("  ") + Spinner("dots", text=Text(f" {self._spinner_label}",
                                                      style=C.dim)).render(time.time())

    def _stop_spin(self) -> None:
        global _ACTIVE_LIVE
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            if _ACTIVE_LIVE is self._live:
                _ACTIVE_LIVE = None
            self._live = None

    # -- events ------------------------------------------------------------
    def handle(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "status":
            self._start_spin(ev.get("text") or "working")
        elif t == "plan":
            self._stop_spin()
            for line in (ev.get("text") or "").splitlines():
                console.print(Text("  " + line.strip()[:110], style=C.dim))
            self._start_spin("working")
        elif t == "thought":
            if self.show_thinking and ev.get("text"):
                self._stop_spin()
                console.print(Text("  ⋮ " + ev["text"].strip()[:400],
                                   style=f"italic {C.dim}"))
                self._start_spin("thinking")
        elif t == "tool_start":
            self._stop_spin()
            self._pending_tools[ev.get("tool", "")] = time.time()
            self._start_spin(f"{ev.get('tool')} {_fmt_args(ev.get('args') or {}, 44)}")
        elif t == "tool_end":
            self._stop_spin()
            name = ev.get("tool", "")
            dur = time.time() - self._pending_tools.pop(name, time.time())
            icon = "✓" if ev.get("ok") else "✗"
            style = C.ok if ev.get("ok") else C.err
            label = ev.get("text") or name
            console.print(
                Text(f"  {icon} ", style=style)
                + Text(f"{name} ", style=f"bold {C.accent}")
                + Text(label[:96], style=C.dim)
                + Text(f"  {dur:.1f}s", style=C.dim))
            self._start_spin("working")
        elif t == "text":
            chunk = ev.get("text", "")
            if not chunk:
                return
            if not self.text_started:
                self._stop_spin()
                self.text_started = True
                console.print()
            self.buffer.append(chunk)
            console.print(Text(chunk, style="white"), end="")
        elif t == "error":
            self._stop_spin()
            error(ev.get("text", "unknown error"))
        elif t == "done":
            self._stop_spin()
            if self.text_started:
                console.print("\n")
            m = ev.get("meta") or {}
            parts = []
            if m.get("steps"):
                parts.append(f"{m['steps']} step" + ("s" if m["steps"] != 1 else ""))
            if m.get("tools"):
                parts.append(f"{m['tools']} tool call" + ("s" if m["tools"] != 1 else ""))
            if m.get("ttft"):
                parts.append(f"{m['ttft']}s to first token")
            if m.get("tps"):
                parts.append(f"{m['tps']} tok/s")
            if m.get("elapsed"):
                parts.append(f"{m['elapsed']}s total")
            if m.get("thought"):
                parts.append("deep reasoning")
            if parts:
                console.print(Text("  " + " · ".join(parts), style=C.dim))
            console.print()

    @property
    def text(self) -> str:
        return "".join(self.buffer).strip()

    def close(self) -> None:
        self._stop_spin()


YES = {"y", "yes", "ok", "okay", "sure", "go", "do it", "allow", "approve"}
ALWAYS = {"a", "always", "all", "ya", "ya always"}
NO = {"n", "no", "stop", "cancel", "deny", "don't", "dont"}


def confirm_prompt(ev: dict) -> tuple[bool, bool]:
    """Ask the user to approve a gated action. Returns (approve, remember)."""
    # The spinner is almost always running when this fires, and it would eat
    # the answer.
    suspend_live()
    console.print()
    body = Table.grid(padding=(0, 1))
    body.add_column(style=C.dim, justify="right")
    body.add_column()
    body.add_row("action", Text(ev.get("tool", "?"), style=f"bold {C.warn}"))
    args = ev.get("args") or {}
    for k, v in list(args.items())[:6]:
        s = str(v)
        body.add_row(k, Text(s[:300] + ("…" if len(s) > 300 else ""), style="white"))
    body.add_row("risk", Text(f"{ev.get('risk')} · {ev.get('category')}", style=C.warn))
    body.add_row("why", Text(ev.get("reason", ""), style=C.dim))
    console.print(Panel(body, title="[bold]confirmation required[/bold]",
                        border_style=C.warn, padding=(0, 2)))
    while True:
        try:
            ans = console.input(
                f"  [bold {C.warn}]allow?[/] [dim]\\[[/][bold]y[/][dim]] yes  "
                f"[dim]\\[[/][bold]a[/][dim]] always this kind  "
                f"[dim]\\[[/][bold]n[/][dim]] no  ›[/] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return False, False
        if ans in ALWAYS:
            return True, True
        if ans in YES:
            return True, False
        if ans in NO or ans == "":
            return False, False
        # Anything else is a typo, not a decision — ask again rather than
        # silently treating it as "no".
        console.print(f"  [dim]didn't catch that — type y, a or n[/dim]")


def show_status(st: dict) -> None:
    t = Table.grid(padding=(0, 2))
    t.add_column(style=C.dim, justify="right")
    t.add_column(style="white")
    for k, v in st.items():
        t.add_row(str(k), str(v))
    console.print(Panel(t, title="status", border_style=C.accent, padding=(0, 2)))


HELP = f"""
[bold {C.accent}]Talking to JARVIS[/]
  Just type. It will use tools when it needs to.

[bold {C.accent}]Commands[/]
  /help              this message
  /voice             switch to voice mode (wake word: "Hey JARVIS")
  /listen            capture one spoken command
  /mute  /unmute     turn speech output off / on
  /memory            what JARVIS remembers
  /forget <text>     remove a memory
  /undo              reverse the last file change JARVIS made
  /history           recent file changes
  /auto [off]        run long tasks without stopping to confirm
  /new               start a fresh conversation
  /backend [name]    show or switch the model: claude | local | cloud
  /status            daemon and model status
  /tools             list available tools
  /think             toggle showing the reasoning trace
  /stop              interrupt the current task
  /quit              exit (daemon keeps running)
  /kill              exit and stop the daemon
"""


def help_panel() -> None:
    console.print(Panel(HELP.strip(), border_style=C.dim, padding=(1, 2)))
