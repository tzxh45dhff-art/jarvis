"""
The text the user has selected, in whatever app is in front - and putting new
text in its place.

"Rewrite this politely", "translate this to Hindi", "fix the grammar": the
selection rides along with the request, and the answer can replace it where
it is. Reading goes through the accessibility API (instant, no side effects);
apps that do not expose their selection that way (some web pages, Electron
apps) get a copy-and-restore fallback that leaves the clipboard as it was.
Password fields are never read.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time

log = logging.getLogger("jarvis.selection")

# Requests that are about "this" text: worth looking for a selection. Anything
# else ignores it - a selection left over from an hour ago must not leak into
# an unrelated question.
_EXPLICIT = re.compile(r"\b(selected|selection|highlighted)\b", re.I)
_VERB = re.compile(
    r"\b(rewrite|reword|rephrase|translate|summari[sz]e|explain|fix|correct|proofread|"
    r"shorten|expand|improve|polish|simplify|formali[sz]e|paraphrase|reply to|respond to|"
    r"make|turn|convert|check|define|tl;?dr)\b", re.I)
_THIS = re.compile(r"\b(this|these|that|it|the text|above|the paragraph|the sentence|the email|"
                   r"the message|the line)\b", re.I)
_WHAT_DOES = re.compile(r"\bwhat (does|do) (this|these|that) (mean|say)\b|\bwhat is this\b", re.I)


def wanted(request: str) -> bool:
    """Only requests that are plainly about the selected text: "rewrite this",
    "translate the selected text", "what does this mean". The copy fallback
    presses Cmd+C, so ordinary questions ("is it raining?") must never qualify."""
    t = request or ""
    return bool(_EXPLICIT.search(t) or _WHAT_DOES.search(t)
                or (_VERB.search(t) and _THIS.search(t)))


def _ax():
    import ApplicationServices as AS
    return AS


def _attr(e, name):
    try:
        err, v = _ax().AXUIElementCopyAttributeValue(e, name, None)
        return v if err == 0 else None
    except Exception:
        return None


def _focused():
    AS = _ax()
    sw = AS.AXUIElementCreateSystemWide()
    return _attr(sw, "AXFocusedUIElement")


def _front_app() -> str:
    try:
        from .context import front_window
        return front_window()[0]
    except Exception:
        return ""


def get(allow_copy: bool = True) -> tuple[str, str]:
    """(app, selected text), or (app, "") when nothing is selected."""
    app = _front_app()
    try:
        e = _focused()
        if e is not None:
            if str(_attr(e, "AXSubrole") or "") == "AXSecureTextField":
                return app, ""
            v = _attr(e, "AXSelectedText")
            if isinstance(v, str) and v.strip():
                return app, v
            # Safari / WebKit: the selection is a text-marker range.
            rng = _attr(e, "AXSelectedTextMarkerRange")
            if rng is not None:
                AS = _ax()
                err, s = AS.AXUIElementCopyParameterizedAttributeValue(
                    e, "AXStringForTextMarkerRange", rng, None)
                if err == 0 and isinstance(s, str) and s.strip():
                    return app, s
    except Exception as e:                                    # noqa: BLE001
        log.debug("AX selection failed: %s", e)
    if allow_copy:
        return app, _copy_selection()
    return app, ""


def _pb():
    from AppKit import NSPasteboard
    return NSPasteboard.generalPasteboard()


def _key(code: int, cmd: bool = True) -> None:
    import Quartz as Q
    for down in (True, False):
        ev = Q.CGEventCreateKeyboardEvent(None, code, down)
        if cmd:
            Q.CGEventSetFlags(ev, Q.kCGEventFlagMaskCommand)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)
        time.sleep(0.01)


def _copy_selection() -> str:
    """Press ⌘C, read what arrived, and put the old clipboard back."""
    try:
        from AppKit import NSPasteboardTypeString
        pb = _pb()
        before_count = pb.changeCount()
        saved = pb.stringForType_(NSPasteboardTypeString)
        _key(8)                                           # c
        deadline = time.time() + 0.45
        while time.time() < deadline and pb.changeCount() == before_count:
            time.sleep(0.03)
        if pb.changeCount() == before_count:
            return ""                                     # nothing was selected
        text = pb.stringForType_(NSPasteboardTypeString) or ""
        _restore_clipboard(saved)
        return str(text)
    except Exception as e:                                # noqa: BLE001
        log.debug("copy fallback failed: %s", e)
        return ""


def _restore_clipboard(saved) -> None:
    from AppKit import NSPasteboardTypeString
    pb = _pb()
    pb.clearContents()
    if saved is not None:
        pb.setString_forType_(saved, NSPasteboardTypeString)
    # Tell clipboard managers (ours included) this was not a real copy.
    pb.setString_forType_("", "org.nspasteboard.TransientType")


def replace(text: str) -> tuple[bool, str]:
    """Put `text` where the selection is. Returns (done, how)."""
    try:
        e = _focused()
        if e is not None and str(_attr(e, "AXSubrole") or "") != "AXSecureTextField":
            AS = _ax()
            before = _attr(e, "AXValue")
            err = AS.AXUIElementSetAttributeValue(e, "AXSelectedText", text)
            after = _attr(e, "AXValue")
            if err == 0 and isinstance(after, str) and after != before and text[:40] in after:
                return True, "accessibility"
    except Exception as ex:                               # noqa: BLE001
        log.debug("AX replace failed: %s", ex)
    # Paste, then put the user's clipboard back.
    try:
        from AppKit import NSPasteboardTypeString
        pb = _pb()
        saved = pb.stringForType_(NSPasteboardTypeString)
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        pb.setString_forType_("", "org.nspasteboard.TransientType")
        _key(9)                                           # v
        time.sleep(0.35)
        _restore_clipboard(saved)
        return True, "paste"
    except Exception as ex:                               # noqa: BLE001
        return False, str(ex)
