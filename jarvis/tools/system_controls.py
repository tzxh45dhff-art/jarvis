"""
Display brightness, dark mode and Wi-Fi - set exactly, then read back.

These used to be improvised: asked for "brightness to max", the model reached
for whatever shell trick it could think of and the screen went to 0. A setting
is now changed through the API that owns it, and the value is read back
afterwards - so "set to 100%" is only reported when the display says 100%.
"""
from __future__ import annotations

import ctypes
import re
import subprocess

from ..safety.policy import Risk, Verdict
from .registry import REGISTRY, ToolResult

_DS = "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices"


# ----------------------------------------------------------------- brightness
def _ds():
    lib = ctypes.cdll.LoadLibrary(_DS)
    lib.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32,
                                                 ctypes.POINTER(ctypes.c_float)]
    lib.DisplayServicesGetBrightness.restype = ctypes.c_int
    lib.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
    lib.DisplayServicesSetBrightness.restype = ctypes.c_int
    lib.DisplayServicesCanChangeBrightness.argtypes = [ctypes.c_uint32]
    lib.DisplayServicesCanChangeBrightness.restype = ctypes.c_bool
    return lib


def _displays() -> list[int]:
    """Displays whose brightness macOS can set (the built-in screen)."""
    import Quartz
    res = Quartz.CGGetOnlineDisplayList(16, None, None)
    ids = res[1] if isinstance(res, tuple) else []
    lib = _ds()
    return [int(d) for d in ids if lib.DisplayServicesCanChangeBrightness(int(d))]


def get_brightness() -> int | None:
    lib = _ds()
    for d in _displays():
        v = ctypes.c_float()
        if lib.DisplayServicesGetBrightness(d, ctypes.byref(v)) == 0:
            return round(v.value * 100)
    return None


def parse_level(level, current: int | None) -> int:
    """
    What a person or a model means by a brightness/volume request.

    "max", "full", "100%" -> 100; "min", "lowest" -> 0; "up"/"brighter" -> +10;
    "+25" -> +25 relative; a bare number is a percentage. Anything unreadable
    raises rather than defaulting - a silent 0 is how the screen went black.
    """
    s = str(level).strip().lower().rstrip("%").strip()
    cur = current if current is not None else 50
    words = {"max": 100, "maximum": 100, "full": 100, "highest": 100, "brightest": 100,
             "min": 0, "minimum": 0, "lowest": 0, "dimmest": 0, "half": 50}
    if s in words:
        return words[s]
    if s in ("up", "brighter", "increase", "higher", "more"):
        return min(100, cur + 10)
    if s in ("down", "dimmer", "darker", "decrease", "lower", "less"):
        return max(0, cur - 10)
    m = re.fullmatch(r"([+-])\s*(\d+(?:\.\d+)?)", s)
    if m:
        delta = float(m.group(2)) * (1 if m.group(1) == "+" else -1)
        return int(max(0, min(100, round(cur + delta))))
    m = re.fullmatch(r"\d+(?:\.\d+)?", s)
    if m:
        v = float(s)
        if 0 < v <= 1 and "." in s:           # 0.8 means 80%, not 1%
            v *= 100
        return int(max(0, min(100, round(v))))
    raise ValueError(f"can't read {level!r} as a brightness level - use a "
                     f"percentage, 'max', 'min', 'up' or 'down'")


@REGISTRY.tool(
    "set_brightness",
    "Set the screen brightness and confirm it. level: a percentage 0-100, or "
    "'max' (100), 'min' (0), 'up' / 'down' (10 steps), or relative '+20' / "
    "'-20'. For 'brightness to max' pass 'max'. Works on the built-in display.",
    {"type": "object", "properties": {
        "level": {"type": "string", "description": "e.g. 'max', '70', 'up', '-20'"}},
     "required": ["level"]},
    risk=Risk.LOW, category="media")
def set_brightness(level: str):
    try:
        lib, displays = _ds(), _displays()
    except Exception as e:
        return ToolResult(False, error=f"brightness control unavailable: {e}")
    if not displays:
        return ToolResult(False, error="no display whose brightness macOS can set "
                                       "(external monitors need their own buttons)")
    before = get_brightness()
    try:
        target = parse_level(level, before)
    except ValueError as e:
        return ToolResult(False, error=str(e))
    for d in displays:
        lib.DisplayServicesSetBrightness(d, ctypes.c_float(target / 100.0))
    import time
    time.sleep(0.15)
    after = get_brightness()
    if after is None or abs(after - target) > 2:
        return ToolResult(False, content={"before": before, "after": after,
                                          "target": target},
                          error=f"asked for {target}% but the display reports "
                                f"{after}%")
    return ToolResult(True, content={"before": before, "brightness": after,
                                     "verified": True},
                      display=f"brightness {before}% -> {after}%")


# ------------------------------------------------------------------ dark mode
def _osa(script: str) -> tuple[bool, str]:
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=15)
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def _dark() -> bool | None:
    ok, out = _osa('tell application "System Events" to tell appearance '
                   'preferences to get dark mode')
    return (out == "true") if ok else None


@REGISTRY.tool(
    "set_dark_mode",
    "Turn macOS dark mode on or off (or toggle it), and confirm the result.",
    {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["on", "off", "toggle"]}},
     "required": ["mode"]},
    risk=Risk.LOW, category="media")
def set_dark_mode(mode: str):
    before = _dark()
    if before is None:
        return ToolResult(False, error="could not read the appearance setting "
                                       "(needs Automation permission for System Events)")
    want = (not before) if mode == "toggle" else (mode == "on")
    if want != before:
        ok, out = _osa('tell application "System Events" to tell appearance '
                       f'preferences to set dark mode to {"true" if want else "false"}')
        if not ok:
            return ToolResult(False, error=f"could not change dark mode: {out}")
    after = _dark()
    if after != want:
        return ToolResult(False, error="dark mode did not change")
    return ToolResult(True, content={"dark_mode": after, "verified": True},
                      display=f"dark mode {'on' if after else 'off'}"
                              + ("" if want != before else " (already)"))


# ---------------------------------------------------------------------- Wi-Fi
def _wifi_device() -> str | None:
    out = subprocess.run(["networksetup", "-listallhardwareports"],
                         capture_output=True, text=True, timeout=10).stdout
    m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\S+)", out)
    return m.group(1) if m else None


def _wifi_on(dev: str) -> bool | None:
    out = subprocess.run(["networksetup", "-getairportpower", dev],
                         capture_output=True, text=True, timeout=10).stdout
    return None if ":" not in out else out.strip().endswith("On")


def _assess_wifi(args: dict) -> Verdict:
    if str(args.get("on", "")).lower() in ("false", "off", "0"):
        return Verdict(Risk.HIGH, "turns Wi-Fi off - JARVIS's cloud model needs "
                                  "the internet and will stop answering",
                       category="system_pref", needs_confirm=True)
    return Verdict(Risk.LOW, "turns Wi-Fi on", category="media")


@REGISTRY.tool(
    "set_wifi",
    "Turn Wi-Fi on or off and confirm it. Turning it off disconnects JARVIS's "
    "cloud model, so it asks first.",
    {"type": "object", "properties": {"on": {"type": "boolean"}},
     "required": ["on"]},
    risk=Risk.MEDIUM, category="system_pref", assess=_assess_wifi)
def set_wifi(on: bool):
    dev = _wifi_device()
    if not dev:
        return ToolResult(False, error="no Wi-Fi hardware found")
    if _wifi_on(dev) == bool(on):
        return ToolResult(True, content={"wifi": bool(on)},
                          display=f"Wi-Fi already {'on' if on else 'off'}")
    r = subprocess.run(["networksetup", "-setairportpower", dev, "on" if on else "off"],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return ToolResult(False, error=(r.stderr or "could not change Wi-Fi").strip())
    import time
    time.sleep(0.8)
    now = _wifi_on(dev)
    if now != bool(on):
        return ToolResult(False, error=f"Wi-Fi is still {'on' if now else 'off'}")
    return ToolResult(True, content={"wifi": now, "verified": True},
                      display=f"Wi-Fi {'on' if now else 'off'}")
