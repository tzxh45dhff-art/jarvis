"""
Mouse, keyboard and application control via native Quartz CGEvents.

CGEvent is used rather than a wrapper library: it is already present through
pyobjc, has no extra dependency, and is markedly faster and more precise.

All input synthesis is gated behind the "control" safety category, because it
can click buttons in other applications.
"""
from __future__ import annotations

import os
import subprocess
import time

from ..safety.policy import Risk, Verdict
from .registry import REGISTRY, ToolResult

# macOS virtual key codes for keys that have no printable character.
KEYCODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "esc": 53, "forwarddelete": 117,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
    "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37,
    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
    "n": 45, "m": 46, ".": 47, "`": 50,
}
_APP_INDEX: list[str] | None = None

# What people call apps, versus what the bundles are called.
_APP_ALIASES = {
    "vscode": "visual studio code", "vs code": "visual studio code",
    "code": "visual studio code", "chrome": "google chrome",
    "brave": "brave browser", "edge": "microsoft edge",
    "word": "microsoft word", "excel": "microsoft excel",
    "powerpoint": "microsoft powerpoint", "outlook": "microsoft outlook",
    "teams": "microsoft teams", "settings": "system settings",
    "preferences": "system settings", "appstore": "app store",
    "activity monitor": "activity monitor", "terminal": "terminal",
    "itunes": "music", "apple music": "music", "photoshop": "adobe photoshop",
    "antigravity": "antigravity ide", "postgres": "postgres",
}

# How long the pointer takes to travel to a target, so the user can see it.
CURSOR_GLIDE = 0.0 if os.environ.get("JARVIS_CURSOR") == "instant" else 0.28

MODIFIERS = {"cmd": 1 << 20, "command": 1 << 20, "shift": 1 << 17,
             "alt": 1 << 19, "option": 1 << 19, "ctrl": 1 << 18,
             "control": 1 << 18, "fn": 1 << 23}


def _q():
    import Quartz
    return Quartz


# ------------------------------------------------------------ input permission
#
# macOS throws away synthetic mouse and keyboard events from a process that
# lacks Accessibility permission - silently. CGEventPost returns nothing, so
# without this check every click and keystroke "succeeded" while the screen
# never changed. The permission belongs to the app that launched JARVIS (the
# daemon inherits it from Terminal, or from the companion app), not to Python.

class InputBlocked(RuntimeError):
    """Raised instead of posting events macOS would discard."""


_AX_LIB = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_TRUST = {"ok": False, "at": 0.0, "prompted": False}
_LAUNCHERS = {
    "com.apple.Terminal": "Terminal", "com.googlecode.iterm2": "iTerm",
    "dev.warp.Warp-Stable": "Warp", "com.mitchellh.ghostty": "Ghostty",
    "net.kovidgoyal.kitty": "kitty", "org.alacritty": "Alacritty",
    "com.microsoft.VSCode": "Visual Studio Code",
    "com.anthropic.claudefordesktop": "Claude",
    "com.example.jarvis.companion": "JARVIS Companion",
}
_TERM_PROGRAMS = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm",
                  "WarpTerminal": "Warp", "ghostty": "Ghostty",
                  "vscode": "your code editor"}


def launcher_app() -> str:
    """The app macOS holds responsible for this process's permissions."""
    bid = os.environ.get("__CFBundleIdentifier", "")
    if bid in _LAUNCHERS:
        return _LAUNCHERS[bid]
    if bid:
        try:
            from AppKit import NSWorkspace
            url = NSWorkspace.sharedWorkspace() \
                .URLForApplicationWithBundleIdentifier_(bid)
            if url is not None:
                return os.path.splitext(os.path.basename(str(url.path())))[0]
        except Exception:
            pass
    return _TERM_PROGRAMS.get(os.environ.get("TERM_PROGRAM", ""),
                              "the app you start JARVIS from")


def _ask_for_access() -> None:
    """Show macOS's own 'allow this app to control your computer' dialog."""
    try:
        import ctypes

        import objc
        from Foundation import NSDictionary
        lib = ctypes.cdll.LoadLibrary(_AX_LIB)
        lib.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        lib.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        opts = NSDictionary.dictionaryWithObject_forKey_(
            True, "AXTrustedCheckOptionPrompt")
        lib.AXIsProcessTrustedWithOptions(objc.pyobjc_id(opts))
    except Exception:
        pass


def input_allowed() -> bool:
    """
    Whether macOS will deliver our mouse and keyboard events.

    A grant is cached for a minute (one tool call posts dozens of events); a
    refusal is re-checked every time, so turning the setting on takes effect
    immediately. If the check itself is unavailable, input is allowed - the
    old behaviour - rather than blocking on a guess.
    """
    now = time.time()
    if _TRUST["ok"] and now - _TRUST["at"] < 60:
        return True
    try:
        import ctypes
        lib = ctypes.cdll.LoadLibrary(_AX_LIB)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        ok = bool(lib.AXIsProcessTrusted())
    except Exception:
        return True
    if not ok and not _TRUST["prompted"]:
        _TRUST["prompted"] = True          # once per run, not on every click
        _ask_for_access()
    _TRUST.update(ok=ok, at=now)
    return ok


def blocked_message() -> str:
    app = launcher_app()
    return (f"macOS is blocking JARVIS from using the mouse and keyboard: {app} "
            f"needs Accessibility permission. Turn it on in System Settings > "
            f"Privacy & Security > Accessibility (if {app} is already listed and "
            f"on, switch it off and on again - macOS can keep a stale grant after "
            f"an update). Nothing was clicked or typed.")


def _post(ev) -> None:
    if not input_allowed():
        raise InputBlocked(blocked_message())
    _q().CGEventPost(_q().kCGHIDEventTap, ev)


def _screen_size() -> tuple[int, int]:
    Q = _q()
    d = Q.CGMainDisplayID()
    return int(Q.CGDisplayPixelsWide(d)), int(Q.CGDisplayPixelsHigh(d))


def _mouse_pos() -> tuple[float, float]:
    Q = _q()
    p = Q.CGEventGetLocation(Q.CGEventCreate(None))
    return float(p.x), float(p.y)



# Where the user is talking to JARVIS. Typing into these by default would put
# JARVIS's keystrokes into its own conversation.
_LAUNCHER_APPS = {"terminal", "iterm2", "iterm", "warp", "ghostty", "kitty",
                  "alacritty", "wezterm", "claude"}


def focus_app(app: str, timeout: float = 3.0) -> tuple[bool, str]:
    """
    Bring a running app to the front and confirm it actually got there.

    `open -b` is used rather than NSRunningApplication.activate: a background
    process like the daemon is not allowed to steal focus that way on current
    macOS, and the call "succeeds" while nothing moves.
    """
    apps = running_apps()
    found, why = _match(app, apps)
    if why:
        return False, why
    if not found:
        r = subprocess.run(["open", "-a", app], capture_output=True, text=True,
                           timeout=20)
        if r.returncode != 0:
            return False, f"{app} is not running and could not be opened"
        time.sleep(1.0)
        found, _ = _match(app, running_apps())
        if not found:
            return False, f"{app} did not start"
    rec = found[0]
    if _norm(frontmost_app(fresh=True)) == _norm(rec["name"]):
        return True, rec["name"]
    cmd = (["open", "-b", rec["bundle_id"]] if rec["bundle_id"]
           else ["open", "-a", rec["path"] or rec["name"]])
    subprocess.run(cmd, capture_output=True, timeout=10)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _norm(frontmost_app(fresh=True)) == _norm(rec["name"]):
            time.sleep(0.12)          # let the window take key status
            return True, rec["name"]
        time.sleep(0.08)
    return False, (f"could not bring {rec['name']} to the front (the front app "
                   f"is {frontmost_app(fresh=True) or 'unknown'})")


def _target_app(app: str) -> tuple[bool, str]:
    """Focus the named app, or refuse to type blind into the launcher."""
    if app:
        return focus_app(app)
    front = frontmost_app(fresh=True)
    if _norm(front) in _LAUNCHER_APPS:
        return False, (f"the app in front is {front} - where you are talking to "
                       f"me. Pass app=... to say which app to type into.")
    return True, front


def _snapshot() -> dict:
    """The app in front, its window and any open dialog, right now."""
    out: dict = {}
    try:
        out["front_app"] = frontmost_app(fresh=True)
    except Exception:
        return out
    try:
        from . import ax
        if ax.available() and out["front_app"]:
            appel, _ = ax._app(out["front_app"])
            win = ax._front_window(appel)
            if win is not None:
                title = ax._str(ax._attr(win, "AXTitle"))
                if title:
                    out["window"] = title
                d = ax._dialog_of(win)
                if d:
                    out["dialog"] = d
    except Exception:
        pass
    return out


def observe(settle: float = 0.25) -> dict:
    """
    What the user would see right now: the app in front, its window, and any
    dialog or sheet that is open.

    Attached to every raw mouse and keyboard action. Without it the model acted
    blind - a click that opened a save sheet, or a shortcut that went to the
    wrong window, looked identical to one that worked, and the next step was
    built on a guess. Never raises.

    `settle` is a ceiling, not a fixed wait: the screen is sampled until two
    looks agree. Most actions have settled after ~80 ms, so this costs about
    half of the flat 250 ms it replaced, on every click and keystroke.
    """
    deadline = time.time() + max(0.0, settle)
    time.sleep(min(0.08, max(0.0, settle)))
    prev = _snapshot()
    while time.time() < deadline:
        time.sleep(0.05)
        cur = _snapshot()
        if cur == prev:
            return cur
        prev = cur
    return prev


def _on_screen(x: float, y: float) -> str | None:
    """None when (x, y) is on some display; otherwise why not."""
    try:
        Q = _q()
        res = Q.CGGetActiveDisplayList(16, None, None)
        ids = res[1] if isinstance(res, tuple) else []
        for d in ids:
            b = Q.CGDisplayBounds(d)
            if b.origin.x <= x < b.origin.x + b.size.width and \
                    b.origin.y <= y < b.origin.y + b.size.height:
                return None
        w, h = _screen_size()
        return (f"({x}, {y}) is off-screen - the main display is {w}x{h} points. "
                f"Get real coordinates from find_on_screen or ui_elements.")
    except Exception:
        return None

# Apps where a stray keystroke or click can send something irreversible.
_SENSITIVE_APPS = {
    "messages", "mail", "whatsapp", "slack", "discord", "telegram", "signal",
    "outlook", "spark", "airmail", "teams", "zoom", "messenger", "x", "twitter",
    "terminal", "iterm2", "iterm",
}
_FRONT_CACHE = {"name": "", "at": 0.0}
_FRONT_TTL = 2.0


def frontmost_app(fresh: bool = False) -> str:
    """
    The app in front. Cached briefly - control actions arrive in bursts - but
    `fresh=True` asks again, which focus checks need: a stale answer is how
    keystrokes end up in the wrong window.
    """
    now = time.time()
    if not fresh and now - _FRONT_CACHE["at"] < _FRONT_TTL:
        return _FRONT_CACHE["name"]
    try:
        # Launch services answers in ~20 ms with no Automation permission.
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if asn:
            # The record starts with the app's name in quotes:
            #   "TextEdit" ASN:0x0-0x5ae5ae: (in front)
            # (`-only name` prints a different, unparseable shape - that
            # mismatch once made every focus check report "unknown", so every
            # keystroke aimed at a named app was refused.)
            info = subprocess.run(["lsappinfo", "info", asn],
                                  capture_output=True, text=True,
                                  timeout=5).stdout
            m = __import__("re").match(r'\s*"([^"]+)"', info)
            if m:
                name = m.group(1).replace("\u200e", "").strip()
                _FRONT_CACHE.update(name=name, at=now)
                return name
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first '
             'application process whose frontmost is true'],
            capture_output=True, text=True, timeout=6)
        name = (r.stdout or "").strip()
    except Exception:
        name = ""
    _FRONT_CACHE.update(name=name, at=now)
    return name


# Keystrokes that commit something in a messaging app. Typing a draft is
# harmless and reversible; pressing Return is what actually sends it.
_COMMITTING_KEYS = {"return", "enter", "cmd+return", "cmd+enter",
                    "shift+return", "shift+enter"}


def _glide(x: int, y: int, duration: float | None = None) -> None:
    """
    Move the pointer visibly rather than teleporting it.

    A synthesised jump is instant, which makes automation impossible to follow —
    the screen just changes. Interpolating the move with an ease-in-out curve
    lets the user watch what JARVIS is doing and, more usefully, see *where* it
    is about to click before it clicks. Set JARVIS_CURSOR=instant to disable.
    """
    Q = _q()
    if duration is None:
        duration = CURSOR_GLIDE
    sx, sy = _mouse_pos()
    dist = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
    if duration <= 0 or dist < 3:
        _post(Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y), 0))
        return
    # shorter hops finish sooner; long ones still cap at `duration`
    duration = min(duration, max(0.08, dist / 2600.0 + 0.10))
    steps = max(6, min(48, int(duration * 120)))
    for i in range(1, steps + 1):
        t = i / steps
        t = 3 * t * t - 2 * t * t * t          # smoothstep
        _post(Q.CGEventCreateMouseEvent(
            None, Q.kCGEventMouseMoved,
            (sx + (x - sx) * t, sy + (y - sy) * t), 0))
        time.sleep(duration / steps)


def _control_verdict(args: dict) -> Verdict:
    """
    Synthetic input is routine and is not gated: clicking around and typing are
    reversible, and prompting for every keystroke makes the assistant unusable.

    The one exception is the keystroke that actually commits — pressing Return
    in a messaging app sends the message, and that cannot be undone. So the gate
    is on *that*, not on input in general.
    """
    app = args.get("app") or frontmost_app()
    sensitive = _norm(app) in _SENSITIVE_APPS
    keys = str(args.get("keys", "")).strip().lower().replace(" ", "")
    commits = keys in _COMMITTING_KEYS

    if sensitive and commits:
        return Verdict(Risk.HIGH,
                       f"presses {keys} in {app}, which sends what is in the "
                       f"compose field — that cannot be recalled",
                       category="send", needs_confirm=True)
    return Verdict(Risk.MEDIUM,
                   f"synthesises input into {app or 'the focused app'}",
                   category="ui_input", needs_confirm=False)


@REGISTRY.tool(
    "mouse_move",
    "Move the mouse pointer to absolute screen coordinates.",
    {"type": "object", "properties": {
        "x": {"type": "integer"}, "y": {"type": "integer"}},
     "required": ["x", "y"]},
    risk=Risk.HIGH, category="control", assess=_control_verdict, parallel_safe=False)
def mouse_move(x: int, y: int):
    _glide(x, y)
    return ToolResult(True, content={"x": x, "y": y}, display=f"moved to ({x}, {y})")


@REGISTRY.tool(
    "click",
    "Click at exact screen coordinates. Only use this when you already know the position — from find_on_screen or inspect_ui. Do not guess coordinates from a screenshot; use click_text instead, which locates the target first.",
    {"type": "object", "properties": {
        "x": {"type": "integer"}, "y": {"type": "integer"},
        "button": {"type": "string", "enum": ["left", "right"]},
        "double": {"type": "boolean"}},
     "required": ["x", "y"]},
    risk=Risk.HIGH, category="control", assess=_control_verdict, parallel_safe=False)
def click(x: int, y: int, button: str = "left", double: bool = False):
    bad = _on_screen(x, y)
    if bad:
        return ToolResult(False, error=bad + " Nothing was clicked.")
    Q = _q()
    down, up, btn = ((Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGMouseButtonRight)
                     if button == "right" else
                     (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGMouseButtonLeft))
    _glide(x, y)
    time.sleep(0.05)          # a beat on the target, so the click is visible
    for i in range(2 if double else 1):
        d = Q.CGEventCreateMouseEvent(None, down, (x, y), btn)
        if double:
            Q.CGEventSetIntegerValueField(d, Q.kCGMouseEventClickState, i + 1)
        _post(d)
        u = Q.CGEventCreateMouseEvent(None, up, (x, y), btn)
        if double:
            Q.CGEventSetIntegerValueField(u, Q.kCGMouseEventClickState, i + 1)
        _post(u)
        time.sleep(0.04)
    after = observe()
    return ToolResult(True, content={"x": x, "y": y, "button": button, "double": double,
                                     "screen_after": after},
                      display=f"{'double ' if double else ''}{button} click at ({x}, {y})"
                              + (f" -> {after.get('front_app')}" if after.get("front_app") else ""))


@REGISTRY.tool(
    "drag",
    "Press at one point, drag to another, and release.",
    {"type": "object", "properties": {
        "x1": {"type": "integer"}, "y1": {"type": "integer"},
        "x2": {"type": "integer"}, "y2": {"type": "integer"}},
     "required": ["x1", "y1", "x2", "y2"]},
    risk=Risk.HIGH, category="control", assess=_control_verdict, parallel_safe=False)
def drag(x1: int, y1: int, x2: int, y2: int):
    bad = _on_screen(x1, y1) or _on_screen(x2, y2)
    if bad:
        return ToolResult(False, error=bad + " Nothing was dragged.")
    Q = _q()
    _glide(x1, y1)
    time.sleep(0.05)
    _post(Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseDown, (x1, y1),
                                    Q.kCGMouseButtonLeft))
    steps = 24
    for i in range(1, steps + 1):
        _post(Q.CGEventCreateMouseEvent(
            None, Q.kCGEventLeftMouseDragged,
            (x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps),
            Q.kCGMouseButtonLeft))
        time.sleep(0.008)
    _post(Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseUp, (x2, y2),
                                    Q.kCGMouseButtonLeft))
    return ToolResult(True, content={"from": [x1, y1], "to": [x2, y2],
                                     "screen_after": observe()},
                      display=f"dragged ({x1},{y1}) -> ({x2},{y2})")


@REGISTRY.tool(
    "scroll",
    "Scroll the wheel. Positive amount scrolls up, negative scrolls down.",
    {"type": "object", "properties": {
        "amount": {"type": "integer", "description": "Lines; negative = down"},
        "x": {"type": "integer"}, "y": {"type": "integer"}},
     "required": ["amount"]},
    risk=Risk.HIGH, category="control", assess=_control_verdict, parallel_safe=False)
def scroll(amount: int, x: int = -1, y: int = -1):
    Q = _q()
    if x >= 0 and y >= 0:
        _glide(x, y)
        time.sleep(0.03)
    step = 3 if amount > 0 else -3
    for _ in range(abs(amount) // 3 + 1):
        _post(Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine, 1, step))
        time.sleep(0.01)
    return ToolResult(True, content={"amount": amount, "screen_after": observe(0.15)},
                      display=f"scrolled {amount}")


@REGISTRY.tool(
    "type_text",
    "Type literal text with the keyboard into an app. Always pass `app` - it is "
    "brought to the front and confirmed before a single key is sent. Where the "
    "focused field is readable the text is checked afterwards. For a named "
    "field, set_field is more reliable.",
    {"type": "object", "properties": {
        "text": {"type": "string"},
        "app": {"type": "string", "description": "App to type into"}},
     "required": ["text"]},
    risk=Risk.HIGH, category="control", assess=_control_verdict, parallel_safe=False)
def type_text(text: str, app: str = "", verify: bool = True):
    ok, who = _target_app(app)
    if not ok:
        return ToolResult(False, error=who + " Nothing was typed.")
    before = None
    if verify:
        try:
            from .ax import focused_text
            before = focused_text(who)
        except Exception:
            before = None
    Q = _q()
    # Unicode string injection handles any character without keycode mapping.
    for chunk_start in range(0, len(text), 20):
        chunk = text[chunk_start:chunk_start + 20]
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
            Q.CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
            _post(ev)
            time.sleep(0.004)
    out = {"typed": len(text), "app": who, "verified": None}
    after = observe(0.1)
    if after:
        out["screen_after"] = after
    if verify and before is not None:
        time.sleep(0.2)
        from .ax import focused_text
        after = focused_text(who) or ""
        probe = text.strip()[-40:]
        out["verified"] = bool(probe) and probe in after
        if probe and not out["verified"]:
            return ToolResult(False, content=out, error=(
                f"typed into {who}, but the focused field does not contain the "
                f"text afterwards - the keystrokes did not land where intended. "
                f"Check with ui_elements before retrying."))
    return ToolResult(True, content=out,
                      display=f"typed {len(text)} character(s) into {who}"
                              + (" (verified)" if out["verified"] else ""))


# ------------------------------------------------------------------ clipboard
def _clip_backup() -> list[dict]:
    """Everything on the clipboard, every format, so it can be put back."""
    from AppKit import NSPasteboard
    saved = []
    for item in NSPasteboard.generalPasteboard().pasteboardItems() or []:
        saved.append({str(t): item.dataForType_(t) for t in item.types()})
    return saved


def _clip_restore(saved: list[dict]) -> None:
    from AppKit import NSPasteboard, NSPasteboardItem
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    items = []
    for d in saved:
        it = NSPasteboardItem.alloc().init()
        for t, data in d.items():
            if data is not None:
                it.setData_forType_(data, t)
        items.append(it)
    if items:
        pb.writeObjects_(items)


@REGISTRY.tool(
    "paste_text",
    "Paste text into an app exactly as written - the fastest, most exact way to "
    "enter long or multi-line text, and the right way to put code into a code "
    "editor (typing makes editors auto-indent and auto-close brackets). Pass "
    "`app`; replace=true selects everything first. Your clipboard is restored "
    "afterwards.",
    {"type": "object", "properties": {
        "text": {"type": "string"},
        "app": {"type": "string"},
        "replace": {"type": "boolean",
                    "description": "Select all in the focused field first"}},
     "required": ["text"]},
    risk=Risk.HIGH, category="control", assess=_control_verdict, parallel_safe=False)
def paste_text(text: str, app: str = "", replace: bool = False):
    ok, who = _target_app(app)
    if not ok:
        return ToolResult(False, error=who + " Nothing was pasted.")
    from AppKit import NSPasteboard
    saved = _clip_backup()
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, "public.utf8-plain-text")
        if replace:
            press_key(keys="cmd+a", app=who, quiet=True)
            time.sleep(0.08)
        press_key(keys="cmd+v", app=who, quiet=True)
        time.sleep(0.4)            # apps read the clipboard lazily on paste
    finally:
        _clip_restore(saved)
    out = {"pasted": len(text), "app": who, "verified": None,
           "screen_after": observe(0.05)}
    try:
        from .ax import focused_text
        now = focused_text(who)
    except Exception:
        now = None
    if now is not None:
        probe = text.strip()[-40:]
        out["verified"] = bool(probe) and probe in now
    return ToolResult(True, content=out,
                      display=f"pasted {len(text)} character(s) into {who}"
                              + (" (verified)" if out["verified"] else ""))


@REGISTRY.tool(
    "press_key",
    "Press a key or keyboard shortcut in an app, e.g. 'return', 'escape', "
    "'cmd+s', 'down'. Pass `app`. Mac keys: cmd+down / cmd+up = end / start of "
    "a document, cmd+right / cmd+left = end / start of a line. For app commands "
    "choose_menu is more reliable; to add text at the end of a document use "
    "set_field with mode='append'.",
    {"type": "object", "properties": {
        "keys": {"type": "string", "description": "Key or modifier+key combination"},
        "repeat": {"type": "integer"},
        "app": {"type": "string", "description": "App to send it to (brought "
                                                 "to the front first)"}},
     "required": ["keys"]},
    risk=Risk.HIGH, category="control", assess=_control_verdict, parallel_safe=False)
def press_key(keys: str, repeat: int = 1, app: str = "", quiet: bool = False):
    ok, who = _target_app(app)
    if not ok:
        return ToolResult(False, error=who + " No key was pressed.")
    Q = _q()
    parts = [p.strip().lower() for p in keys.replace(" ", "+").split("+") if p.strip()]
    mods = 0
    key = None
    for p in parts:
        if p in MODIFIERS:
            mods |= MODIFIERS[p]
        else:
            key = p
    if key is None:
        return ToolResult(False, error=f"No non-modifier key in {keys!r}")
    # On a Mac, Home/End scroll a text view without moving the cursor - so
    # "press End, then type" puts text in the wrong place. In a text area the
    # user's intent is the cursor, which is cmd+up / cmd+down (cmd+left/right
    # for the line, with ctrl). Remapped only when a text field has focus.
    remapped = ""
    if key in ("end", "home") and not (mods & MODIFIERS["cmd"]):
        try:
            from .ax import _TEXT_ROLES, _app, _attr, _str
            appel, _ = _app(who)
            foc = _attr(appel, "AXFocusedUIElement")
            if foc is not None and _str(_attr(foc, "AXRole")) in _TEXT_ROLES:
                # one-line field: end of the line; document: end of the text
                line = _str(_attr(foc, "AXRole")) != "AXTextArea"
                key = {"end": "right" if line else "down",
                       "home": "left" if line else "up"}[key]
                mods = (mods & ~MODIFIERS["ctrl"]) | MODIFIERS["cmd"]
                remapped = (f"{keys} does not move the cursor on a Mac; pressed "
                            f"cmd+{key} instead (cursor to the "
                            f"{'end' if key in ('down', 'right') else 'start'})")
        except Exception:
            pass
    code = KEYCODES.get(key)
    if code is None:
        return ToolResult(False, error=f"Unknown key {key!r}. "
                                       f"Use type_text for literal characters.")
    for _ in range(max(1, min(repeat, 100))):
        d = Q.CGEventCreateKeyboardEvent(None, code, True)
        Q.CGEventSetFlags(d, mods)
        _post(d)
        time.sleep(0.012)
        u = Q.CGEventCreateKeyboardEvent(None, code, False)
        Q.CGEventSetFlags(u, mods)
        _post(u)
        time.sleep(0.012)
    after = {} if quiet else observe(0.2)
    return ToolResult(True, content={"keys": keys, "repeat": repeat, "app": who,
                                     "screen_after": after,
                                     **({"note": remapped} if remapped else {})},
                      display=f"pressed {keys} in {who}"
                              + (f" -> {after['dialog']}" if after.get("dialog") else ""))


def installed_apps() -> list[str]:
    """Every installed application name, cached for the process lifetime."""
    global _APP_INDEX
    if _APP_INDEX is not None:
        return _APP_INDEX
    from pathlib import Path
    names: set[str] = set()
    roots = ["/Applications", "/System/Applications",
             "/System/Applications/Utilities", "/Applications/Utilities",
             str(Path.home() / "Applications")]
    for root in roots:
        p = Path(root)
        if not p.is_dir():
            continue
        try:
            for entry in p.iterdir():
                if entry.suffix == ".app":
                    names.add(entry.stem)
                elif entry.is_dir():
                    for sub in entry.iterdir():
                        if sub.suffix == ".app":
                            names.add(sub.stem)
        except OSError:
            continue
    _APP_INDEX = sorted(names)
    return _APP_INDEX


def resolve_app(name: str) -> tuple[str | None, list[str]]:
    """
    Map what the user called an app to what it is actually called.

    People say "antigravity" for "Antigravity IDE" and "chrome" for
    "Google Chrome". Failing on an exact-name miss makes the assistant look
    broken when the app is sitting right there.
    """
    import difflib
    q = (name or "").strip().lower()
    if not q:
        return None, []
    q = _APP_ALIASES.get(q, q)
    apps = installed_apps()
    lower = {a.lower(): a for a in apps}
    if q in lower:
        return lower[q], []
    # "vscode" for "Visual Studio Code", "vs code" for the same
    squashed = q.replace(" ", "").replace("-", "")
    for a in apps:
        if a.lower().replace(" ", "").replace("-", "") == squashed:
            return a, []
    # initials: "vs code" -> Visual Studio Code, "as" -> App Store
    def initials(s):
        return "".join(w[0] for w in s.split() if w)
    qtok = q.split()
    for a in apps:
        words = a.split()
        if len(qtok) == len(words) and all(
                w.lower().startswith(t) for w, t in zip(words, qtok)):
            return a, []
        if qtok and initials(a).lower() == qtok[0] and len(qtok) == 1:
            return a, []
    # every query word appears somewhere in the app name
    if len(qtok) > 1:
        subset = [a for a in apps
                  if all(t in a.lower().replace(" ", "") for t in qtok)]
        if len(subset) == 1:
            return subset[0], []
    starts = [a for a in apps if a.lower().startswith(q)]
    contains = [a for a in apps if q in a.lower()]
    words = [a for a in apps if q in a.lower().split()]
    for bucket in (starts, words, contains):
        if len(bucket) == 1:
            return bucket[0], []
        if len(bucket) > 1:
            return None, bucket[:6]
    # A loose cutoff here is worse than no match: "vscode" scores highly against
    # "Xcode", and opening the wrong application is a real mistake.
    close = difflib.get_close_matches(q, [a.lower() for a in apps], n=4, cutoff=0.86)
    if len(close) == 1:
        return lower[close[0]], []
    if close:
        return None, [lower[c] for c in close]
    return None, []


@REGISTRY.tool(
    "open_app",
    "Open or focus a macOS application by name, e.g. 'Safari', 'Antigravity', "
    "'VS Code'. Partial and informal names are resolved against what is "
    "actually installed. Optionally open a file or folder with it.",
    {"type": "object", "properties": {
        "name": {"type": "string"},
        "file": {"type": "string", "description": "Optional file, folder or URL to open with it"}},
     "required": ["name"]},
    risk=Risk.LOW, category="control", parallel_safe=False,
    assess=lambda a: Verdict(Risk.LOW, "opens an application", category="read"))
def open_app(name: str, file: str = ""):
    resolved, options = resolve_app(name)
    if resolved is None and options:
        return ToolResult(False, error=(
            f"{name!r} matches several installed apps: {', '.join(options)}. "
            f"Call open_app again with the exact one."))
    target = resolved or name
    args = [os.path.expanduser(file)] if file else []
    r = subprocess.run(["open", "-a", target] + args,
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        hint = ""
        if resolved is None:
            near = [a for a in installed_apps()
                    if name.strip().lower()[:4] in a.lower()][:8]
            hint = (f" Installed apps with a similar name: {', '.join(near)}."
                    if near else
                    " No installed application has a name like that — "
                    "check list_installed_apps.")
        return ToolResult(False, error=(err or f"could not open {target!r}") + hint)
    time.sleep(0.8)
    return ToolResult(True,
                      content={"app": target, "asked_for": name,
                               "opened_file": file or None},
                      display=f"opened {target}" + (f" with {file}" if file else ""))


@REGISTRY.tool(
    "list_installed_apps",
    "List the applications installed on this Mac. Use when an app name does not "
    "resolve, to find what it is actually called.",
    {"type": "object", "properties": {
        "filter": {"type": "string", "description": "Optional substring to match"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def list_installed_apps(filter: str = ""):
    apps = installed_apps()
    if filter:
        f = filter.lower()
        apps = [a for a in apps if f in a.lower()]
    return ToolResult(True, content={"apps": apps, "count": len(apps)},
                      display=f"{len(apps)} installed app(s)"
                              + (f" matching {filter!r}" if filter else ""))


# --------------------------------------------------------------- running apps
#
# Three traps, all hit while building this:
#
# * NSWorkspace.runningApplications is a snapshot that is only refreshed by the
#   Cocoa run loop. The daemon has none, so the list freezes at first use: it
#   would "quit" apps closed hours ago and never see ones opened since.
#   `lsappinfo` asks launch services directly - fresh every call, ~40 ms, and
#   no permission needed.
# * NSRunningApplication.isTerminated is updated the same way, so it stays False
#   forever here. Whether an app really quit is checked by its PID instead.
# * terminate() returns True as soon as the quit is *sent*. An app with an
#   unsaved document shows a save dialog and keeps running, so "sent" is never
#   reported as "done" - the old bulk-quit path claimed success while nothing
#   closed.

# Apps a sweep ("close everything except ...") must never take down: the
# terminal JARVIS is talking through, the desktop it lives in, and itself.
# Naming one of these explicitly still works - that is the user's call.
_NEVER_SWEEP = {
    "finder", "claude", "terminal", "iterm2", "iterm", "warp", "ghostty",
    "kitty", "alacritty", "wezterm", "jarvis companion", "loginwindow",
}
_QUIT_WAIT = 8.0       # long enough for a normal quit, short enough to report


def _norm(name: str) -> str:
    """Comparable form of an app name: no invisible marks, case or .app."""
    n = (name or "").replace("‎", "").replace("‏", "").strip().lower()
    return n[:-4] if n.endswith(".app") else n


def running_apps() -> list[dict]:
    """Regular (Dock) applications that are running right now."""
    import re as _re
    try:
        out = subprocess.run(["lsappinfo", "list"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return []
    apps = []
    for block in _re.split(r'(?m)^\s*\d+\) ', out)[1:]:
        # Launch services keeps records for apps that already exited (marked
        # "exited-with-subordinates"); listing those would "quit" an app that
        # is not running and report it closed.
        if 'type="Foreground"' not in block or "(exited" in block:
            continue
        m_name = _re.match(r'"(.*?)"', block)
        m_pid = _re.search(r'\bpid = (\d+)', block)
        if not (m_name and m_pid) or not _pid_alive(int(m_pid.group(1))):
            continue
        m_bid = _re.search(r'bundleID="([^"]*)"', block)
        m_path = _re.search(r'bundle path="([^"]*)"', block)
        apps.append({
            "name": m_name.group(1).replace("\u200e", "").strip(),
            "pid": int(m_pid.group(1)),
            "bundle_id": m_bid.group(1) if m_bid else "",
            "path": m_path.group(1) if m_path else "",
        })
    return apps


def _match(query: str, apps: list[dict]) -> tuple[list[dict], str]:
    """Resolve what a person called an app to the running app(s) it means."""
    q = _norm(_APP_ALIASES.get(_norm(query), query))
    exact = [a for a in apps if q in (_norm(a["name"]), a["bundle_id"].lower(),
                                     _norm(os.path.basename(a["path"])))]
    if exact:
        return exact, ""
    partial = [a for a in apps if q and q in _norm(a["name"])]
    if len(partial) == 1:
        return partial, ""
    if len(partial) > 1:
        return [], (f"'{query}' matches several apps: "
                    + ", ".join(a["name"] for a in partial))
    return [], ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _own_pids() -> set[int]:
    """JARVIS's own process and the one that launched it. The daemon uses
    AppKit, so it is listed as a foreground app called "Python": "quit
    everything" used to send JARVIS itself a quit (and force=True would have
    killed it mid-request)."""
    return {os.getpid(), os.getppid()}


def _plan_quit(names: list[str] | None, all_except: list[str] | None) -> dict:
    """Work out exactly which apps a quit request would touch."""
    plan = {"targets": [], "not_running": [], "ambiguous": [], "kept": []}
    mine = _own_pids()
    apps = []
    for a in running_apps():
        if a["pid"] in mine:
            if all_except is not None:
                plan["kept"].append(f"{a['name']} (JARVIS itself)")
            continue
        apps.append(a)
    if all_except is not None:
        keep_q = [_norm(_APP_ALIASES.get(_norm(n), n)) for n in all_except]
        for a in apps:
            n = _norm(a["name"])
            hit = any(k and (k == n or k in n or
                             k == a["bundle_id"].lower()) for k in keep_q)
            if hit or n in _NEVER_SWEEP:
                plan["kept"].append(a["name"])
            else:
                plan["targets"].append(a)
    for q in names or []:
        found, why = _match(q, apps)
        if why:
            plan["ambiguous"].append(why)
        elif not found:
            plan["not_running"].append(q)
        for a in found:
            if all(a["pid"] != t["pid"] for t in plan["targets"]):
                plan["targets"].append(a)
    return plan


def _assess_quit(args: dict) -> Verdict:
    names = args.get("names") or ([args["name"]] if args.get("name") else [])
    plan = _plan_quit(names, args.get("all_except"))
    if not plan["targets"]:
        return Verdict(Risk.SAFE, "nothing to quit", category="read")
    listed = ", ".join(t["name"] for t in plan["targets"])
    how = ("force-quits (unsaved work is lost)" if args.get("force")
           else "quits (apps with unsaved work will ask to save)")
    return Verdict(Risk.HIGH, f"{how}: {listed}", category="control",
                   needs_confirm=True)


@REGISTRY.tool(
    "quit_apps",
    "Quit running applications and verify they actually closed. Use this - not "
    "run_shell or AppleScript - for any request to quit, close or kill apps. "
    "Give `names` to quit specific apps, or `all_except` to quit everything "
    "except the listed apps (Finder, Claude and the terminal running JARVIS are "
    "always kept). Reports which apps quit, which are still open (usually a "
    "save dialog waiting for the user) and which were not running.",
    {"type": "object", "properties": {
        "names": {"type": "array", "items": {"type": "string"},
                  "description": "Apps to quit, e.g. ['Safari', 'Notes']"},
        "all_except": {"type": "array", "items": {"type": "string"},
                       "description": "Quit every app EXCEPT these"},
        "force": {"type": "boolean",
                  "description": "Force-quit apps that do not respond. Loses "
                                 "unsaved work - only if the user asked"}},
     "required": []},
    risk=Risk.HIGH, category="control", parallel_safe=False,
    assess=_assess_quit)
def quit_apps(names: list[str] | None = None, all_except: list[str] | None = None,
              force: bool = False):
    from AppKit import NSRunningApplication
    if not names and all_except is None:
        return ToolResult(False, error="say which apps to quit (names) or which "
                                       "to keep (all_except)")
    plan = _plan_quit(names, all_except)
    if plan["ambiguous"] and not plan["targets"]:
        return ToolResult(False, error="; ".join(plan["ambiguous"]))

    sent: list[dict] = []
    for t in plan["targets"]:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(t["pid"])
        if app is None:                     # quit between planning and now
            continue
        (app.forceTerminate if force else app.terminate)()
        sent.append(t)

    deadline = time.time() + _QUIT_WAIT
    while time.time() < deadline and any(_pid_alive(t["pid"]) for t in sent):
        time.sleep(0.1)
    quit_ok = [t["name"] for t in sent if not _pid_alive(t["pid"])]
    still = [t["name"] for t in sent if _pid_alive(t["pid"])]

    content = {"quit": quit_ok, "still_open": still,
               "not_running": plan["not_running"], "kept": plan["kept"]}
    if plan["ambiguous"]:
        content["ambiguous"] = plan["ambiguous"]
    bits = []
    if quit_ok:
        bits.append(f"quit {len(quit_ok)}: {', '.join(quit_ok)}")
    if still:
        bits.append(f"still open: {', '.join(still)}")
    if plan["not_running"]:
        bits.append(f"not running: {', '.join(plan['not_running'])}")
    if not bits:
        bits.append("nothing to quit")
    display = "; ".join(bits)
    if still:
        # The error is all the model sees of a failed call, so it must also say
        # what DID quit - otherwise a sweep that closed nine apps reads as
        # "nothing worked" and gets retried.
        return ToolResult(
            False, content=content, display=display,
            error=((f"Quit {len(quit_ok)}: {', '.join(quit_ok)}. " if quit_ok else "")
                   + f"{', '.join(still)} did not quit within {_QUIT_WAIT:.0f}s - "
                   "most likely a save dialog is waiting. Tell the user; do not "
                   "force-quit unless they ask."))
    return ToolResult(True, content=content, display=display)


@REGISTRY.tool(
    "quit_app",
    "Quit one application gracefully and verify it closed (it may prompt to "
    "save). For several apps at once use quit_apps.",
    {"type": "object", "properties": {"name": {"type": "string"}},
     "required": ["name"]},
    risk=Risk.HIGH, category="control", parallel_safe=False,
    assess=_assess_quit)
def quit_app(name: str):
    return quit_apps(names=[name])


@REGISTRY.tool(
    "list_apps",
    "List the applications running right now, and which one is in front.",
    {"type": "object", "properties": {}, "required": []},
    risk=Risk.SAFE, category="read")
def list_apps():
    apps = [a["name"] for a in running_apps()]
    if not apps:
        return ToolResult(False, error="could not list running apps")
    front = frontmost_app()
    return ToolResult(True, content={"apps": apps, "frontmost": front},
                      display=f"{len(apps)} app(s) running; front: {front}")


@REGISTRY.tool(
    "inspect_ui",
    "Older name for ui_elements - use ui_elements instead, which lists the same "
    "controls with ids that press_element and set_field accept.",
    {"type": "object", "properties": {
        "app": {"type": "string", "description": "Application name"}},
     "required": ["app"]},
    risk=Risk.SAFE, category="read", slow=True)
def inspect_ui(app: str):
    try:
        from . import ax
        if ax.available():
            return ax.ui_elements(app)
    except Exception:
        pass
    script = f'''
    tell application "System Events"
        tell process "{app}"
            set out to ""
            try
                set w to front window
                set out to out & "WINDOW: " & (name of w) & linefeed
                repeat with e in (entire contents of w)
                    try
                        set r to role of e
                        if r is in {{"AXButton","AXTextField","AXTextArea","AXCheckBox","AXRadioButton","AXMenuButton","AXPopUpButton","AXLink","AXStaticText","AXComboBox"}} then
                            set nm to ""
                            try
                                set nm to description of e
                            end try
                            if nm is "" then
                                try
                                    set nm to name of e
                                end try
                            end if
                            if nm is "" then
                                try
                                    set nm to value of e as string
                                end try
                            end if
                            set pos to position of e
                            set sz to size of e
                            set out to out & r & " | " & nm & " | " & (item 1 of pos) & "," & (item 2 of pos) & " " & (item 1 of sz) & "x" & (item 2 of sz) & linefeed
                        end if
                    end try
                end repeat
            on error errMsg
                set out to "ERROR: " & errMsg
            end try
            return out
        end tell
    end tell
    '''
    # An app that is not frontmost often reports no windows at all — but
    # activating one that *is* already frontmost dismisses any open menu or
    # popover, which is usually the very thing being inspected.
    if frontmost_app().lower() != app.lower():
        subprocess.run(["osascript", "-e",
                        f'tell application "{app}" to activate'],
                       capture_output=True, timeout=10)
        time.sleep(0.35)
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=45)
    txt = (r.stdout or "").strip()
    if r.returncode != 0 or txt.startswith("ERROR:"):
        err = txt or (r.stderr or "").strip()
        # Electron and Catalyst apps (VS Code, Slack, Discord…) frequently
        # expose no usable AX window. Retrying never fixes it, so say so and
        # name the tool that *will* work — otherwise the model keeps trying.
        no_window = "window 1" in err or "Can't get" in err
        return ToolResult(False, error=(
            f"{app} does not expose a readable accessibility tree "
            f"({err[:120]}). This will not succeed on retry. "
            f"Use see_screen to look at it visually instead, and click by "
            f"coordinates."
            if no_window else
            f"Could not read the UI of {app}: {err}. This needs Accessibility "
            f"permission (System Settings > Privacy & Security > Accessibility) "
            f"for the app running JARVIS."))
    lines = [l for l in txt.splitlines() if l.strip()][:200]
    return ToolResult(True, content={"app": app, "elements": lines},
                      display=f"inspected {app} UI ({len(lines)} elements)")


@REGISTRY.tool(
    "screen_info",
    "Get screen dimensions and the current mouse position.",
    {"type": "object", "properties": {}, "required": []},
    risk=Risk.SAFE, category="read")
def screen_info():
    w, h = _screen_size()
    mx, my = _mouse_pos()
    return ToolResult(True, content={"width": w, "height": h,
                                     "mouse": {"x": int(mx), "y": int(my)}},
                      display=f"screen {w}x{h}, mouse at ({int(mx)}, {int(my)})")
