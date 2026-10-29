"""
Native app control through the macOS accessibility API.

The reliable way to operate a Mac app is the same way VoiceOver does: ask the
app for its controls by name and act on them directly. That beats every
alternative the agent had before -

  * reading screen text (OCR) cannot see icon buttons like "x", misses anything
    behind another window, and gives coordinates that go stale;
  * clicking coordinates moves the user's mouse and lands on whatever happens
    to be there now;
  * AppleScript's `entire contents` reads the same tree but takes 10+ seconds
    per window and cannot press anything.

Measured: Calculator's whole window walks in ~140 ms, with every button named
("Multiply", "Equals", "7") and the display text readable.

Every action here is checked: a press reports what the window looks like
afterwards (title, any dialog that appeared), and text set into a field is read
back - a field that does not contain the text afterwards is a failure, not a
success.

Requires Accessibility permission for the app that launched JARVIS, the same
grant mouse and keyboard control needs.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

from ..safety.policy import Risk, Verdict
from . import control as C
from .registry import REGISTRY, ToolResult

_ACTIONABLE = {
    "AXButton", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXMenuButton", "AXLink", "AXComboBox", "AXSlider",
    "AXIncrementor", "AXDisclosureTriangle", "AXSegmentedControl", "AXTab",
    "AXCell", "AXSwitch", "AXColorWell", "AXDateField",
}
_TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
_READABLE = {"AXStaticText", "AXHeading"}
_SKIP_SUBTREES = {"AXMenuBar", "AXMenu"}       # menus go through choose_menu

_MAX_NODES = 2500
_WALK_BUDGET = 2.5                              # seconds
_MAX_LISTED = 160
_MAX_TEXTS = 40
_CACHE_TTL = 120.0

# Pressing these can do something that cannot be taken back.
_RISKY = re.compile(
    r"\b(send|delete|remove|erase|pay|buy|purchase|place order|checkout|"
    r"transfer|publish|post|empty trash|sign out|log ?out|shut ?down|restart|"
    r"uninstall|discard|don'?t save|move to (the )?trash|unsubscribe|"
    r"reset|format disk)\b", re.I)

_LOCK = threading.RLock()
_LAST: dict[str, dict] = {}          # app -> {"at", "elems": {id: (elem, info)}}


# --------------------------------------------------------------------- basics
def _ax():
    import ApplicationServices as AS
    return AS


def available() -> bool:
    try:
        _ax()
        return True
    except Exception:
        return False


def _attr(e, name):
    try:
        err, v = _ax().AXUIElementCopyAttributeValue(e, name, None)
        return v if err == 0 else None
    except Exception:
        return None


def _str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    try:
        return str(v).strip()
    except Exception:
        return ""


def _label(e, role: str = "") -> str:
    if role in _READABLE:
        return _str(_attr(e, "AXValue")) or _str(_attr(e, "AXDescription"))
    for a in ("AXTitle", "AXDescription", "AXPlaceholderValue", "AXHelp"):
        s = _str(_attr(e, a))
        if s:
            return s
    t = _attr(e, "AXTitleUIElement")
    if t is not None:
        s = _str(_attr(t, "AXValue")) or _str(_attr(t, "AXTitle"))
        if s:
            return s
    return _str(_attr(e, "AXIdentifier"))


def _center(e) -> tuple[int, int] | None:
    AS = _ax()
    try:
        pv, sv = _attr(e, "AXPosition"), _attr(e, "AXSize")
        if pv is None or sv is None:
            return None
        ok1, p = AS.AXValueGetValue(pv, AS.kAXValueCGPointType, None)
        ok2, s = AS.AXValueGetValue(sv, AS.kAXValueCGSizeType, None)
        if ok1 and ok2:
            return int(p.x + s.width / 2), int(p.y + s.height / 2)
    except Exception:
        pass
    return None


def _app(app: str):
    """(AX application element, running-app record) or raise a clear error."""
    apps = C.running_apps()
    found, why = C._match(app, apps)
    if why:
        raise LookupError(why)
    if not found:
        raise LookupError(f"{app} is not running - open it with open_app first")
    rec = found[0]
    el = _ax().AXUIElementCreateApplication(rec["pid"])
    try:
        _ax().AXUIElementSetMessagingTimeout(el, 2.0)   # a hung app must not
    except Exception:                                   # hang the agent
        pass
    return el, rec


def _front_window(appel):
    w = _attr(appel, "AXFocusedWindow")
    if w is None:
        ws = _attr(appel, "AXWindows") or []
        w = ws[0] if ws else None
    return w


def _dialog_of(win) -> str:
    """Title of a sheet or dialog in front of the user, if any."""
    if win is None:
        return ""
    if _str(_attr(win, "AXRole")) == "AXSheet":
        return "sheet: " + (_sheet_summary(win) or "untitled")
    sub = _str(_attr(win, "AXSubrole"))
    if sub in ("AXDialog", "AXSystemDialog", "AXFloatingWindow"):
        return _str(_attr(win, "AXTitle")) or sub[2:].lower()
    for ch in _attr(win, "AXChildren") or []:
        if _str(_attr(ch, "AXRole")) == "AXSheet":
            return "sheet: " + (_sheet_summary(ch) or "untitled")
    return ""


def _sheet_summary(sheet) -> str:
    """A few words saying what a sheet is - its labels and buttons."""
    words, queue, n = [], [sheet], 0
    while queue and n < 300 and len(words) < 8:
        e = queue.pop(0)
        n += 1
        r = _str(_attr(e, "AXRole"))
        if r in ("AXStaticText", "AXButton"):
            lab = _label(e, r)
            if lab and lab not in words and len(lab) < 40:
                words.append(lab)
        queue.extend(_attr(e, "AXChildren") or [])
    return " / ".join(words)[:140]


def _walk(appel) -> tuple[list[tuple], dict]:
    """Breadth-first walk of the front window. Returns (items, window info)."""
    win = _front_window(appel)
    info = {"window": _str(_attr(win, "AXTitle")) if win is not None else "",
            "dialog": _dialog_of(win), "no_window": win is None}
    items: list[tuple] = []
    if win is None:
        return items, info
    t0 = time.time()
    queue = [win]
    n = 0
    while queue and n < _MAX_NODES and time.time() - t0 < _WALK_BUDGET:
        e = queue.pop(0)
        n += 1
        role = _str(_attr(e, "AXRole"))
        if role in _SKIP_SUBTREES:
            continue
        if role in _ACTIONABLE or role in _READABLE:
            items.append((e, role))
        queue.extend(_attr(e, "AXChildren") or [])
    info["truncated"] = bool(queue)
    return items, info


def _describe(e, role: str) -> dict:
    d = {"role": role[2:].lower() if role.startswith("AX") else role,
         "label": _label(e, role)}
    if role in _TEXT_ROLES or role in ("AXCheckBox", "AXRadioButton", "AXSlider",
                                       "AXPopUpButton", "AXComboBox", "AXSwitch"):
        v = _attr(e, "AXValue")
        if v is not None and not isinstance(v, (list, tuple)):
            s = _str(v)
            d["value"] = s if len(s) <= 120 else s[:117] + "..."
    if _attr(e, "AXEnabled") is False:
        d["disabled"] = True
    if _attr(e, "AXFocused") is True:
        d["focused"] = True
    return d


def _remember(app: str, mapping: dict) -> None:
    with _LOCK:
        _LAST[C._norm(app)] = {"at": time.time(), "elems": mapping}


def _cached(app: str, eid: str):
    with _LOCK:
        c = _LAST.get(C._norm(app))
        if c and time.time() - c["at"] < _CACHE_TTL:
            return c["elems"].get(eid)
    return None


def _resolve(app: str, element: str, roles: set[str] | None = None,
             occurrence: int = 1):
    """
    Find one element by the id ui_elements gave it ("e12") or by its name.

    Exact name beats prefix beats substring; among equals, `occurrence` picks.
    An ambiguous name is an error listing the candidates - guessing which of
    two "OK" buttons to press is exactly the failure this module exists to stop.
    """
    appel, rec = _app(app)
    if re.fullmatch(r"e\d+", element.strip()):
        hit = _cached(app, element.strip())
        if hit is not None and _attr(hit[0], "AXRole") is not None:
            return hit[0], hit[1], appel, rec
        raise LookupError(f"{element} is stale - call ui_elements('{app}') again")
    items, _ = _walk(appel)
    want = element.strip().lower().rstrip(".…")
    cands = [(e, r, _label(e, r)) for e, r in items
             if r in _ACTIONABLE and (roles is None or r in roles)]
    for test in (lambda l: l == want, lambda l: l.startswith(want),
                 lambda l: want in l):
        hits = [(e, r, l) for e, r, l in cands
                if test(l.lower().rstrip(".…")) and l]
        if hits:
            if len(hits) > 1 and occurrence == 1 and \
                    len({l for _, _, l in hits}) > 1:
                names = ", ".join(sorted({f'"{l}"' for _, _, l in hits})[:8])
                raise LookupError(f"'{element}' matches several controls in "
                                  f"{rec['name']}: {names}. Use the exact name "
                                  f"or its id from ui_elements.")
            idx = min(max(occurrence, 1), len(hits)) - 1
            e, r, _ = hits[idx]
            return e, r, appel, rec
    sample = ", ".join(sorted({f'"{l}"' for _, _, l in cands if l})[:40])
    raise LookupError(f"no control named '{element}' in {rec['name']}'s front "
                      f"window. Controls there: {sample or 'none readable'}")


def _after(appel) -> dict:
    """What the user sees now - so the caller can tell whether it worked."""
    time.sleep(0.3)
    win = _front_window(appel)
    out = {"window": _str(_attr(win, "AXTitle")) if win is not None else ""}
    d = _dialog_of(win)
    if d:
        out["dialog"] = d
    return out


def _verdict_for(app: str, label: str) -> Verdict:
    if label and _RISKY.search(label):
        sens = C._norm(app) in C._SENSITIVE_APPS
        return Verdict(Risk.HIGH, f"presses '{label}' in {app}, which may not "
                                  f"be undoable",
                       category="send" if sens and "send" in label.lower()
                       else "control", needs_confirm=True)
    return Verdict(Risk.MEDIUM, f"operates {app}", category="ui_input")


def _label_from_args(args: dict) -> str:
    el = str(args.get("element") or args.get("field") or args.get("path") or "")
    if re.fullmatch(r"e\d+", el.strip()):
        hit = _cached(args.get("app", ""), el.strip())
        if hit is not None:
            return _label(hit[0], hit[1])
    return el.split(">")[-1].strip()


# ---------------------------------------------------------------------- tools
@REGISTRY.tool(
    "ui_elements",
    "List the controls in an app's front window - buttons, text fields, "
    "checkboxes, menus buttons, links - by name, with an id for each, plus the "
    "visible text and any open dialog or sheet. Fast (well under a second) and "
    "works when the window is partly hidden. Use it to see what you can press "
    "before acting, and again afterwards to check the result.",
    {"type": "object", "properties": {
        "app": {"type": "string"},
        "filter": {"type": "string",
                   "description": "Only controls whose name or role contains this"}},
     "required": ["app"]},
    risk=Risk.SAFE, category="read")
def ui_elements(app: str, filter: str = ""):
    try:
        appel, rec = _app(app)
    except LookupError as e:
        return ToolResult(False, error=str(e))
    items, info = _walk(appel)
    if info["no_window"]:
        return ToolResult(False, error=(
            f"{rec['name']} has no open window. If it should, use choose_menu "
            f"(e.g. 'File > New') or see_screen."))
    if not items and not info["dialog"]:
        # Electron/Catalyst apps often expose nothing. Say so once, with the
        # tool that will work, instead of letting the model retry this forever.
        return ToolResult(False, error=(
            f"{rec['name']} exposes no readable controls (common for Electron "
            f"apps). Use see_screen / find_on_screen and click_text instead."))
    f = filter.strip().lower()
    mapping, lines, texts = {}, [], []
    for i, (e, role) in enumerate(items):
        d = _describe(e, role)
        if role in _READABLE:
            if d["label"] and len(texts) < _MAX_TEXTS:
                texts.append(d["label"][:160])
            continue
        if f and f not in d["label"].lower() and f not in d["role"]:
            continue
        eid = f"e{i}"
        mapping[eid] = (e, role)
        if len(lines) < _MAX_LISTED:
            bits = [eid, d["role"], f'"{d["label"]}"' if d["label"] else "(unlabeled)"]
            if "value" in d:
                bits.append(f'value="{d["value"]}"')
            if d.get("disabled"):
                bits.append("disabled")
            if d.get("focused"):
                bits.append("focused")
            lines.append(" ".join(bits))
    _remember(rec["name"], mapping)
    content = {"app": rec["name"], "window": info["window"], "controls": lines,
               "text": texts}
    if info["dialog"]:
        content["dialog"] = info["dialog"]
    if info.get("truncated") or len(mapping) > len(lines):
        content["note"] = "list truncated - pass a filter to narrow it"
    disp = f"{rec['name']}: {len(lines)} controls" + \
        (f", dialog open ({info['dialog']})" if info["dialog"] else "")
    return ToolResult(True, content=content, display=disp)


@REGISTRY.tool(
    "press_element",
    "Press a button, checkbox, tab, link or other control in an app, by its "
    "name or by the id ui_elements gave it. Acts directly through "
    "accessibility - no mouse movement - and reports the window afterwards "
    "(including any dialog that appeared). Prefer this to clicking coordinates.",
    {"type": "object", "properties": {
        "app": {"type": "string"},
        "element": {"type": "string",
                    "description": "Control name (e.g. 'Multiply', 'Save') or id ('e12')"},
        "occurrence": {"type": "integer",
                       "description": "Which one, if several share the name (1 = first)"}},
     "required": ["app", "element"]},
    risk=Risk.MEDIUM, category="ui_input", parallel_safe=False,
    assess=lambda a: _verdict_for(a.get("app", ""), _label_from_args(a)))
def press_element(app: str, element: str, occurrence: int = 1):
    try:
        e, role, appel, rec = _resolve(app, element, occurrence=occurrence)
    except LookupError as err:
        return ToolResult(False, error=str(err))
    label = _label(e, role) or element
    if _attr(e, "AXEnabled") is False:
        return ToolResult(False, error=f"'{label}' in {rec['name']} is disabled "
                                       f"right now")
    AS = _ax()
    err_a, actions = AS.AXUIElementCopyActionNames(e, None)
    actions = list(actions or []) if err_a == 0 else []
    for act in ("AXPress", "AXConfirm", "AXPick", "AXOpen", "AXShowMenu"):
        if act in actions or (not actions and act == "AXPress"):
            if AS.AXUIElementPerformAction(e, act) == 0:
                after = _after(appel)
                return ToolResult(True, content={"pressed": label, "app": rec["name"],
                                                 "action": act, **after},
                                  display=f"pressed {label!r} in {rec['name']}"
                                  + (f" -> {after['dialog']}" if after.get("dialog") else ""))
    # Some controls (often inside web views) accept no AX action; a real click
    # at the element's centre is the honest fallback, and it still verifies.
    pt = _center(e)
    if pt is not None:
        C.focus_app(rec["name"])
        r = C.click(x=pt[0], y=pt[1])
        if r.ok:
            after = _after(appel)
            return ToolResult(True, content={"pressed": label, "app": rec["name"],
                                             "action": "click", **after},
                              display=f"clicked {label!r} in {rec['name']}")
        return r
    return ToolResult(False, error=f"'{label}' in {rec['name']} cannot be pressed "
                                   f"(actions: {', '.join(actions) or 'none'})")


@REGISTRY.tool(
    "set_field",
    "Put text into a text field or text area of an app - search boxes, form "
    "fields, a document body - found by its name, placeholder or id. The text "
    "is read back afterwards; if it did not land, this fails rather than "
    "pretending. mode 'replace' (default) or 'append'.",
    {"type": "object", "properties": {
        "app": {"type": "string"},
        "field": {"type": "string",
                  "description": "Field name, placeholder, or id from ui_elements. "
                                 "Use 'focused' for whichever field has focus."},
        "text": {"type": "string"},
        "mode": {"type": "string", "enum": ["replace", "append"]},
        "submit": {"type": "boolean", "description": "Press Return afterwards"}},
     "required": ["app", "field", "text"]},
    risk=Risk.MEDIUM, category="ui_input", parallel_safe=False,
    assess=lambda a: (Verdict(Risk.HIGH, f"types and submits in {a.get('app')}, "
                                         f"which sends it", category="send",
                              needs_confirm=True)
                      if a.get("submit") and C._norm(a.get("app", "")) in C._SENSITIVE_APPS
                      else Verdict(Risk.MEDIUM, f"fills a field in {a.get('app')}",
                                   category="ui_input")))
def set_field(app: str, field: str, text: str, mode: str = "replace",
              submit: bool = False):
    AS = _ax()
    try:
        if field.strip().lower() in ("focused", "current", ""):
            appel, rec = _app(app)
            e = _attr(appel, "AXFocusedUIElement")
            role = _str(_attr(e, "AXRole")) if e is not None else ""
            if e is None or role not in _TEXT_ROLES:
                return ToolResult(False, error=f"no text field has focus in "
                                               f"{rec['name']}; name the field")
        else:
            e, role, appel, rec = _resolve(app, field, roles=_TEXT_ROLES)
    except LookupError as err:
        return ToolResult(False, error=str(err))
    label = _label(e, role) or field
    before = _str(_attr(e, "AXValue"))
    target = (before + text) if mode == "append" else text

    AS.AXUIElementSetAttributeValue(e, "AXFocused", True)
    err_s, settable = AS.AXUIElementIsAttributeSettable(e, "AXValue", None)
    method = "accessibility"
    if err_s == 0 and settable:
        AS.AXUIElementSetAttributeValue(e, "AXValue", target)
        time.sleep(0.15)
    landed = _str(_attr(e, "AXValue"))
    if text.strip() and text.strip() not in landed:
        # Not settable, or the app ignored it (some web and rich-text fields):
        # type it like a person would, then check again.
        method = "keystrokes"
        C.focus_app(rec["name"])
        AS.AXUIElementSetAttributeValue(e, "AXFocused", True)
        time.sleep(0.15)
        if mode == "replace":
            C.press_key(keys="cmd+a", app=rec["name"], quiet=True)
        else:
            C.press_key(keys="cmd+down", app=rec["name"], quiet=True)
        r = C.type_text(text=text, app=rec["name"], verify=False)
        if not r.ok:
            return r
        time.sleep(0.25)
        landed = _str(_attr(e, "AXValue"))
    if text.strip() and text.strip() not in landed:
        return ToolResult(False, content={"field": label, "now_contains": landed[:200]},
                          error=(f"the text did not land in '{label}' in "
                                 f"{rec['name']} - it now contains "
                                 f"{landed[:80]!r}. Nothing was submitted."))
    out = {"field": label, "app": rec["name"], "verified": True, "method": method}
    if submit:
        C.press_key(keys="return", app=rec["name"], quiet=True)
        out.update(_after(appel))
        out["submitted"] = True
    return ToolResult(True, content=out,
                      display=f"set {label!r} in {rec['name']} ({len(text)} chars, "
                              f"verified)" + (" and submitted" if submit else ""))


def _norm_title(s: str) -> str:
    return re.sub(r"(\.\.\.|…)$", "", (s or "").strip()).strip().lower()


@REGISTRY.tool(
    "choose_menu",
    "Run a menu-bar command in an app, e.g. 'File > New', 'Format > Font > "
    "Bold', 'View > Show Sidebar', 'Edit > Select All'. The most reliable way to "
    "trigger any app command. On a wrong name, lists the items that do exist.",
    {"type": "object", "properties": {
        "app": {"type": "string"},
        "path": {"type": "string", "description": "Menu path separated by '>'"}},
     "required": ["app", "path"]},
    risk=Risk.MEDIUM, category="ui_input", parallel_safe=False,
    assess=lambda a: _verdict_for(a.get("app", ""), _label_from_args(a)))
def choose_menu(app: str, path: str):
    parts = [p.strip() for p in re.split(r">|→|/", path) if p.strip()]
    if not parts:
        return ToolResult(False, error="give a menu path like 'File > New'")
    try:
        appel, rec = _app(app)
    except LookupError as err:
        return ToolResult(False, error=str(err))
    ok, msg = C.focus_app(rec["name"])        # menus act on the frontmost app
    if not ok:
        return ToolResult(False, error=msg)
    bar = _attr(appel, "AXMenuBar")
    if bar is None:
        return ToolResult(False, error=f"{rec['name']} has no readable menu bar")

    level = _attr(bar, "AXChildren") or []
    item = None
    walked = []
    for depth, want in enumerate(parts):
        titled = [(it, _str(_attr(it, "AXTitle"))) for it in level]
        titled = [(it, t) for it, t in titled if t]
        w = _norm_title(want)
        item = next((it for it, t in titled if _norm_title(t) == w), None) or \
            next((it for it, t in titled if _norm_title(t).startswith(w)), None) or \
            next((it for it, t in titled if w in _norm_title(t)), None)
        if item is None:
            names = ", ".join(t for _, t in titled if t != "Apple")[:400]
            where = " > ".join(walked) or "the menu bar"
            return ToolResult(False, error=f"no '{want}' in {where} of {rec['name']}. "
                                           f"Items there: {names}")
        walked.append(_str(_attr(item, "AXTitle")))
        if depth < len(parts) - 1:
            sub = _attr(item, "AXChildren") or []
            level = (_attr(sub[0], "AXChildren") or []) if sub else []
            if not level:
                return ToolResult(False, error=f"'{walked[-1]}' in {rec['name']} "
                                               f"has no submenu")
    if _attr(item, "AXEnabled") is False:
        return ToolResult(False, error=f"{' > '.join(walked)} is disabled in "
                                       f"{rec['name']} right now")
    if _ax().AXUIElementPerformAction(item, "AXPress") != 0:
        return ToolResult(False, error=f"could not run {' > '.join(walked)} in "
                                       f"{rec['name']}")
    after = _after(appel)
    return ToolResult(True, content={"ran": " > ".join(walked), "app": rec["name"],
                                     **after},
                      display=f"{rec['name']}: {' > '.join(walked)}"
                      + (f" -> {after['dialog']}" if after.get("dialog") else ""))


def focused_text(app_name: str) -> str | None:
    """The text in the app's focused field, or None when it cannot be read."""
    try:
        appel, _ = _app(app_name)
    except Exception:
        return None
    e = _attr(appel, "AXFocusedUIElement")
    if e is None or _str(_attr(e, "AXRole")) not in _TEXT_ROLES:
        return None
    if _str(_attr(e, "AXSubrole")) == "AXSecureTextField":
        return None                       # password fields never echo
    v = _attr(e, "AXValue")
    return v if isinstance(v, str) else None


# --------------------------------------------------------------- dropdowns
def _menu_items(e) -> list[tuple]:
    kids = _attr(e, "AXChildren") or []
    menu = next((k for k in kids if _str(_attr(k, "AXRole")) == "AXMenu"), None)
    if menu is None:
        return []
    return [(i, _str(_attr(i, "AXTitle"))) for i in (_attr(menu, "AXChildren") or [])
            if _str(_attr(i, "AXTitle"))]


@REGISTRY.tool(
    "select_option",
    "Choose an option from a dropdown or pop-up menu in an app (e.g. 'File "
    "Format' > 'Plain Text', 'Where' > 'Desktop', a settings pop-up). Opens it, "
    "picks the option and confirms it is now selected; on a wrong name, lists "
    "the options that exist.",
    {"type": "object", "properties": {
        "app": {"type": "string"},
        "control": {"type": "string", "description": "Dropdown name or id"},
        "option": {"type": "string"}},
     "required": ["app", "control", "option"]},
    risk=Risk.MEDIUM, category="ui_input", parallel_safe=False,
    assess=lambda a: _verdict_for(a.get("app", ""), str(a.get("option", ""))))
def select_option(app: str, control: str, option: str):
    AS = _ax()
    try:
        e, role, appel, rec = _resolve(
            app, control, roles={"AXPopUpButton", "AXMenuButton", "AXComboBox"})
    except LookupError as err:
        return ToolResult(False, error=str(err))
    label = _label(e, role) or control
    want = _norm_title(option)
    if _norm_title(_str(_attr(e, "AXValue"))) == want:
        return ToolResult(True, content={"control": label, "value": option},
                          display=f"{label} already set to {option!r}")
    items = _menu_items(e)
    if not items:                         # menus are built when opened
        AS.AXUIElementPerformAction(e, "AXPress")
        time.sleep(0.35)
        items = _menu_items(e)
    pick = next((i for i, t in items if _norm_title(t) == want), None) or \
        next((i for i, t in items if _norm_title(t).startswith(want)), None) or \
        next((i for i, t in items if want in _norm_title(t)), None)
    if pick is None:
        # Close only the menu. An Escape keystroke here would also cancel any
        # dialog the dropdown sits in - a Save panel, a settings sheet.
        kids = _attr(e, "AXChildren") or []
        menu = next((k for k in kids if _str(_attr(k, "AXRole")) == "AXMenu"), None)
        if menu is not None and AS.AXUIElementPerformAction(menu, "AXCancel") != 0:
            C.press_key(keys="escape", app=rec["name"], quiet=True)
        names = ", ".join(t for _, t in items)[:400]
        return ToolResult(False, error=f"'{label}' in {rec['name']} has no option "
                                       f"'{option}'. Options: {names or 'none readable'}")
    AS.AXUIElementPerformAction(pick, "AXPress")
    time.sleep(0.3)
    now = _str(_attr(e, "AXValue"))
    if role == "AXPopUpButton" and now and want not in _norm_title(now):
        return ToolResult(False, error=f"picked '{option}' but '{label}' now shows "
                                       f"{now!r}")
    return ToolResult(True, content={"control": label, "value": now or option},
                      display=f"{label} -> {now or option!r}")


# ------------------------------------------------------------- file dialogs
def _panel(appel):
    """The Save/Open panel in front, if one is."""
    win = _front_window(appel)
    if win is None:
        return None
    if _str(_attr(win, "AXRole")) == "AXSheet":
        return win
    for ch in _attr(win, "AXChildren") or []:
        if _str(_attr(ch, "AXRole")) == "AXSheet":
            return ch
    sub = _str(_attr(win, "AXSubrole"))
    return win if sub in ("AXDialog", "AXSystemDialog") else None


def _find(root, roles: set[str], names: tuple[str, ...] = ()) -> list:
    out, queue, n = [], [root], 0
    while queue and n < 1500:
        e = queue.pop(0)
        n += 1
        r = _str(_attr(e, "AXRole"))
        if r in roles:
            lab = _label(e, r).lower().rstrip(":")
            if not names or any(lab == x or lab.startswith(x) for x in names):
                out.append(e)
        queue.extend(_attr(e, "AXChildren") or [])
    return out


@REGISTRY.tool(
    "file_dialog",
    "Complete the Save or Open panel that is showing in an app: goes to the "
    "folder, fills in the name, presses Save/Open, and checks the result (for a "
    "save, that the file now exists). Give the full path, e.g. "
    "'~/Desktop/report.txt'. Will not replace an existing file unless "
    "overwrite is true. Open the panel first (choose_menu 'File > Save').",
    {"type": "object", "properties": {
        "app": {"type": "string"},
        "path": {"type": "string", "description": "Full path to save to or open"},
        "overwrite": {"type": "boolean"}},
     "required": ["app", "path"]},
    risk=Risk.MEDIUM, category="ui_input", parallel_safe=False,
    assess=lambda a: (Verdict(Risk.HIGH, f"may replace {a.get('path')}",
                              category="overwrite", needs_confirm=True)
                      if a.get("overwrite") else
                      Verdict(Risk.MEDIUM, f"saves/opens {a.get('path')}",
                              category="ui_input")))
def file_dialog(app: str, path: str, overwrite: bool = False):
    import os
    AS = _ax()
    try:
        appel, rec = _app(app)
    except LookupError as err:
        return ToolResult(False, error=str(err))
    panel = _panel(appel)
    if panel is None:
        return ToolResult(False, error=f"no Save or Open panel is showing in "
                                       f"{rec['name']} - open one first, e.g. "
                                       f"choose_menu 'File > Save'")
    full = os.path.abspath(os.path.expanduser(path))
    folder, name = os.path.dirname(full), os.path.basename(full)
    name_fields = _find(panel, {"AXTextField"}, ("save as", "name", "export as"))
    saving = bool(name_fields)
    if saving and not os.path.isdir(folder):
        return ToolResult(False, error=f"folder {folder} does not exist")
    if not saving and not os.path.exists(full):
        return ToolResult(False, error=f"{full} does not exist")

    # Go to the folder (or straight to the file, when opening) through the
    # panel's own "Go to Folder" - the one path that works in every panel,
    # sidebar or not, iCloud default or not.
    ok, msg = C.focus_app(rec["name"])
    if not ok:
        return ToolResult(False, error=msg)
    C.press_key(keys="cmd+shift+g", app=rec["name"], quiet=True)
    time.sleep(0.6)
    goto = _attr(appel, "AXFocusedUIElement")
    if goto is None or _str(_attr(goto, "AXRole")) not in _TEXT_ROLES:
        return ToolResult(False, error="the panel's Go to Folder field did not open")
    AS.AXUIElementSetAttributeValue(goto, "AXValue", folder if saving else full)
    time.sleep(0.25)
    C.press_key(keys="return", app=rec["name"], quiet=True)
    time.sleep(0.8)

    panel = _panel(appel) or panel
    if saving:
        fields = _find(panel, {"AXTextField"}, ("save as", "name", "export as"))
        if not fields:
            return ToolResult(False, error="lost the file-name field after "
                                           "changing folder")
        f = fields[0]
        AS.AXUIElementSetAttributeValue(f, "AXFocused", True)
        AS.AXUIElementSetAttributeValue(f, "AXValue", name)
        time.sleep(0.2)
        if _str(_attr(f, "AXValue")) != name:
            return ToolResult(False, error=f"could not set the file name to {name!r}")
    buttons = _find(panel, {"AXButton"}, ("save", "open", "choose", "export"))
    if not buttons:
        return ToolResult(False, error="no Save/Open button in the panel")
    btn = buttons[0]
    if _attr(btn, "AXEnabled") is False:
        return ToolResult(False, error=f"'{_label(btn, 'AXButton')}' is disabled - "
                                       f"the name or location is not accepted")
    AS.AXUIElementPerformAction(btn, "AXPress")
    time.sleep(0.7)

    # An existing file brings up "Replace?" - answer it as instructed.
    after_panel = _panel(appel)
    if after_panel is not None:
        replace = _find(after_panel, {"AXButton"}, ("replace",))
        if replace:
            if overwrite:
                AS.AXUIElementPerformAction(replace[0], "AXPress")
                time.sleep(0.7)
            else:
                cancel = _find(after_panel, {"AXButton"}, ("cancel",))
                if cancel:
                    AS.AXUIElementPerformAction(cancel[0], "AXPress")
                return ToolResult(False, error=f"{full} already exists - not "
                                               f"replaced. Pass overwrite=true "
                                               f"if the user wants it replaced.")

    if saving:
        stem, ext = os.path.splitext(full)
        deadline = time.time() + 4
        landed = None
        while time.time() < deadline and landed is None:
            for cand in (full, *(stem + e for e in (".rtf", ".txt", ".rtfd", ".pdf",
                                                    ".docx", ".html") if e != ext)):
                if os.path.exists(cand):
                    landed = cand
                    break
            time.sleep(0.2)
        if landed is None:
            still = _panel(appel)
            return ToolResult(False, error=(
                f"the file did not appear at {full}"
                + (f" - a dialog is still open: {_dialog_of(_front_window(appel))}"
                   if still is not None else "")))
        out = {"saved": landed, "app": rec["name"]}
        if landed != full:
            out["note"] = (f"saved as {os.path.basename(landed)} - the app chose "
                           f"the extension for its format")
        return ToolResult(True, content=out, display=f"saved {landed}")
    gone = _panel(appel) is None
    return ToolResult(gone, content={"opened": full, "app": rec["name"]},
                      display=f"opened {full}",
                      error=None if gone else "the Open panel is still showing")


# ---------------------------------------------------------------- web pages
#
# Safari and Chromium expose the whole page to accessibility - every link,
# button, field and paragraph, including parts scrolled off screen - with no
# browser setting changed. When "Allow JavaScript from Apple Events" is off this
# is a far better fallback than reading pixels.

def _web_area(appel):
    win = _front_window(appel)
    if win is None:
        return None
    queue, n = [win], 0
    while queue and n < 600:
        e = queue.pop(0)
        n += 1
        if _str(_attr(e, "AXRole")) == "AXWebArea":
            return e
        queue.extend(_attr(e, "AXChildren") or [])
    return None


def web_text(app: str, max_chars: int = 8000) -> str | None:
    """The page's text in reading order, or None if there is no web page."""
    try:
        appel, _ = _app(app)
    except LookupError:
        return None
    root = _web_area(appel)
    if root is None:
        return None
    out, total, last = [], 0, ""
    stack, n, t0 = [root], 0, time.time()
    while stack and n < 8000 and total < max_chars and time.time() - t0 < 5:
        e = stack.pop()
        n += 1
        role = _str(_attr(e, "AXRole"))
        if role in ("AXStaticText", "AXHeading"):
            s = _str(_attr(e, "AXValue")) or _str(_attr(e, "AXDescription"))
            if s and s != last:
                out.append(s)
                total += len(s) + 1
                last = s
        kids = _attr(e, "AXChildren") or []
        stack.extend(reversed(list(kids)))          # depth-first, page order
    return "\n".join(out)[:max_chars]


def web_controls(app: str, contains: str = "", limit: int = 60) -> list[dict] | None:
    try:
        appel, rec = _app(app)
    except LookupError:
        return None
    root = _web_area(appel)
    if root is None:
        return None
    items, queue, n, t0 = [], [root], 0, time.time()
    while queue and n < 6000 and time.time() - t0 < 4:
        e = queue.pop(0)
        n += 1
        role = _str(_attr(e, "AXRole"))
        if role in _ACTIONABLE:
            items.append((e, role))
        queue.extend(_attr(e, "AXChildren") or [])
    mapping, out = {}, []
    want = contains.strip().lower()
    for i, (e, role) in enumerate(items):
        lab = _label(e, role)
        if not lab or (want and want not in lab.lower()):
            continue
        eid = f"w{i}"
        mapping[eid] = (e, role)
        if len(out) < limit:
            out.append({"id": eid, "role": role[2:].lower(), "text": lab[:80]})
    with _LOCK:
        _LAST[C._norm(rec["name"]) + "#web"] = {"at": time.time(), "elems": mapping}
    return out


def press_web(app: str, text: str, occurrence: int = 1) -> ToolResult:
    """Press a link or button on the current page by its text."""
    try:
        appel, rec = _app(app)
    except LookupError as e:
        return ToolResult(False, error=str(e))
    root = _web_area(appel)
    if root is None:
        return ToolResult(False, error=f"no web page in {rec['name']}'s front window")
    want = text.strip().lower()
    cands, queue, n = [], [root], 0
    while queue and n < 6000:
        e = queue.pop(0)
        n += 1
        role = _str(_attr(e, "AXRole"))
        if role in ("AXButton", "AXLink", "AXRadioButton", "AXCheckBox", "AXTab",
                    "AXPopUpButton", "AXMenuButton", "AXMenuItem"):
            cands.append((e, role, _label(e, role)))
        queue.extend(_attr(e, "AXChildren") or [])
    for test in (lambda l: l == want, lambda l: l.startswith(want),
                 lambda l: want in l):
        hits = [(e, r, l) for e, r, l in cands if l and test(l.lower())]
        if hits:
            e, r, l = hits[min(max(occurrence, 1), len(hits)) - 1]
            if _ax().AXUIElementPerformAction(e, "AXPress") == 0:
                time.sleep(0.4)
                return ToolResult(True, content={"pressed": l, "via": "accessibility"},
                                  display=f"pressed {l!r} on the page")
    return ToolResult(False, error=f"no link or button '{text}' on the page")


def set_web_field(app: str, field: str, text: str) -> ToolResult:
    """Fill a field on the current page by its label or placeholder."""
    try:
        appel, rec = _app(app)
    except LookupError as e:
        return ToolResult(False, error=str(e))
    root = _web_area(appel)
    if root is None:
        return ToolResult(False, error=f"no web page in {rec['name']}'s front window")
    want = field.strip().lower()
    queue, n, hit = [root], 0, None
    while queue and n < 6000 and hit is None:
        e = queue.pop(0)
        n += 1
        role = _str(_attr(e, "AXRole"))
        if role in _TEXT_ROLES and want in _label(e, role).lower():
            hit = e
        queue.extend(_attr(e, "AXChildren") or [])
    if hit is None:
        return ToolResult(False, error=f"no field '{field}' on the page")
    AS = _ax()
    AS.AXUIElementSetAttributeValue(hit, "AXFocused", True)
    AS.AXUIElementSetAttributeValue(hit, "AXValue", text)
    time.sleep(0.2)
    landed = _str(_attr(hit, "AXValue"))
    if text.strip() and text.strip() not in landed:
        C.paste_text(text=text, app=rec["name"], replace=True)
        landed = _str(_attr(hit, "AXValue"))
    ok = not text.strip() or text.strip() in landed
    return ToolResult(ok, content={"field": field, "verified": ok, "via": "accessibility"},
                      display=f"filled {field!r} on the page",
                      error=None if ok else f"the text did not land in '{field}'")


def web_editor(app: str):
    """The page's code-editor input, if it has one (Monaco, CodeMirror...)."""
    try:
        appel, _ = _app(app)
    except LookupError:
        return None
    root = _web_area(appel)
    queue, n = ([root] if root is not None else []), 0
    while queue and n < 8000:
        e = queue.pop(0)
        n += 1
        role = _str(_attr(e, "AXRole"))
        if role in ("AXTextArea", "AXTextField"):
            lab = _label(e, role).lower()
            if "editor" in lab or "code" in lab:
                return e
        queue.extend(_attr(e, "AXChildren") or [])
    return None


def select_web_option(app: str, control: str, option: str) -> ToolResult:
    """
    Choose from a website's custom dropdown (LeetCode's language picker, site
    menus): open it, pick the option, and confirm the control now shows it.

    Custom menus render their entries as plain text, which accessibility
    cannot "press" - so the entry is clicked at its real on-screen position.
    """
    try:
        appel, rec = _app(app)
    except LookupError as e:
        return ToolResult(False, error=str(e))
    root = _web_area(appel)
    if root is None:
        return ToolResult(False, error=f"no web page in {rec['name']}")

    def find(roles, text, exact=True):
        out, q, n = [], [root], 0
        while q and n < 8000:
            e = q.pop(0)
            n += 1
            r = _str(_attr(e, "AXRole"))
            if r in roles:
                lab = _label(e, r).strip()
                if (lab.lower() == text.lower()) if exact else (text.lower() in lab.lower()):
                    out.append(e)
            q.extend(_attr(e, "AXChildren") or [])
        return out

    buttons = (find({"AXPopUpButton", "AXMenuButton", "AXButton", "AXComboBox"}, control)
               or find({"AXPopUpButton", "AXMenuButton", "AXComboBox"}, control, exact=False))
    if not buttons:
        return ToolResult(False, error=f"no dropdown labelled '{control}' on the page - "
                                       f"use browser_find to see its current label")
    btn = buttons[0]
    if _label(btn, _str(_attr(btn, "AXRole"))).strip().lower() == option.lower():
        return ToolResult(True, content={"selected": option, "verified": True},
                          display=f"'{option}' was already selected")
    C.focus_app(rec["name"])
    if _ax().AXUIElementPerformAction(btn, "AXPress") != 0:
        pt = _center(btn)
        if pt:
            C.click(*pt)
    time.sleep(0.7)
    # The option is the matching entry that is NOT the button itself; prefer
    # the one that appeared lowest on screen (the open list sits below it).
    cands = [e for e in find({"AXStaticText", "AXMenuItem", "AXButton", "AXRadioButton",
                              "AXCell", "AXRow", "AXGroup", "AXLink"}, option)
             if e != btn]
    pts = [(p, e) for e in cands if (p := _center(e))]
    bp = _center(btn) or (0, 0)
    pts = [(p, e) for p, e in pts if p[1] > bp[1]] or pts
    if not pts:
        C.press_key("escape", app=rec["name"], quiet=True)
        return ToolResult(False, error=f"opened '{control}' but found no option '{option}'")
    (x, y), _ = min(pts, key=lambda pe: abs(pe[0][1] - bp[1]))
    C.click(x, y)
    time.sleep(0.8)
    now = [_label(b, _str(_attr(b, "AXRole"))).strip()
           for b in find({"AXPopUpButton", "AXMenuButton", "AXButton", "AXComboBox"},
                         option)]
    ok = bool(now)
    return ToolResult(ok, content={"selected": option, "verified": ok},
                      display=f"selected '{option}'" if ok else "",
                      error=None if ok else f"clicked '{option}' but the dropdown "
                                            f"does not show it - check with browser_find")
