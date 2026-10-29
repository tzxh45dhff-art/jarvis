"""
Web control through the browser's own JavaScript engine.

Clicking a web page by pixel is the worst option available: the page already
knows where everything is, and can be asked. Driving Netflix, YouTube, Gmail or
any other web app through the DOM is exact, survives layout changes, and does
not depend on what happens to be visible in the viewport.

Safari needs Develop > "Allow JavaScript from Apple Events" enabled once; the
error message says so when it is not.
"""
from __future__ import annotations

import json
import logging
import time

from ..safety.policy import Risk, Verdict
from . import osa
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.browser")

# Both Safari and Chrome refuse scripted JavaScript until a one-time setting is
# turned on. That is the user's call, not ours — but web control must not depend
# on it, so every DOM tool falls back to OCR grounding, which always works.
_JS_BLOCKED_HINT = "blocking scripted JavaScript"


def _js_blocked(err: str) -> bool:
    return _JS_BLOCKED_HINT in (err or "")

CHROMIUM = ("Google Chrome", "Brave Browser", "Arc", "Microsoft Edge")


def _active_browser() -> str | None:
    """An explicitly set default wins over whatever happens to be running."""
    from ..memory.prefs import get_default
    from .macos import running_apps
    pref = get_default("browser", "")
    if pref and osa.app_installed(pref):
        return pref
    up = running_apps()
    for b in ("Safari",) + CHROMIUM:
        if b in up and osa.app_installed(b):
            return b
    return "Safari" if osa.app_installed("Safari") else None


_JS_BLOCKED_AT: dict[str, float] = {}
_JS_OK: set[str] = set()
JS_BLOCK_MEMORY = 600.0        # re-check the setting every ten minutes


def _note_js_blocked(app: str) -> None:
    """Scripting is off in this browser: every later call can skip the attempt,
    and the model is no longer offered browser_js (it always failed, costing
    a whole step to learn that again)."""
    first = app not in _JS_BLOCKED_AT
    _JS_BLOCKED_AT[app] = time.time()
    if first:
        try:
            REGISTRY.defer(["browser_js"])
        except Exception:
            pass


def run_js(code: str, browser: str | None = None, timeout: int = 40):
    """Evaluate JavaScript in the frontmost tab. Returns (ok, result_or_error)."""
    app = browser or _active_browser()
    if not app:
        return False, "No browser is running. Open one first with open_app."
    if time.time() - _JS_BLOCKED_AT.get(app, 0) < JS_BLOCK_MEMORY:
        return False, _JS_BLOCKED_HINT
    if app == "Safari":
        script = '''on run argv
  tell application "Safari"
    set r to (do JavaScript (item 1 of argv) in front document)
    if r is missing value then return ""
    return r as text
  end tell
end run'''
    else:
        script = f'''on run argv
  tell application "{app}"
    set r to (execute active tab of front window javascript (item 1 of argv))
    if r is missing value then return ""
    return r as text
  end tell
end run'''
    r = osa.run(script, code, timeout=timeout)
    if not r.ok:
        err = r.error
        # "Can't get document 1" is an empty browser, not a permission problem —
        # they need different advice, and conflating them sends the user to
        # toggle a setting that was never off.
        if "document 1" in err or "front window" in err or "active tab" in err:
            return False, (f"{app} has no page open. Use browser_open with a URL "
                           f"first.")
        if "Allow JavaScript" in err or "not allowed" in err.lower() or "-1743" in err:
            _note_js_blocked(app)
            return False, (
                f"{app} is blocking scripted JavaScript. Enable it once: "
                + ("Safari > Settings > Advanced > 'Show features for web "
                   "developers', then Develop > 'Allow JavaScript from Apple "
                   "Events'." if app == "Safari" else
                   f"{app} > View > Developer > 'Allow JavaScript from Apple "
                   f"Events'."))
        return False, err
    return True, r.out



# ------------------------------------------------------------- one JARVIS tab
#
# `open -a Safari <url>` makes a new tab every time. An evening of "open the
# next LeetCode problem" left a dozen tabs, ~400 MB each. JARVIS now keeps one
# tab of its own per browser and navigates it. It is only ever reused while it
# is still on the site JARVIS left it on, so a tab the user has moved to
# something else is never overwritten - a fresh one is opened instead.

_OWNED: dict[str, dict] = {}

_SAFARI_OPEN = '''on run argv
  set u to item 1 of argv
  set wid to item 2 of argv
  set idx to item 3 of argv
  set lastHost to item 4 of argv
  tell application "Safari"
    if wid is not "" and lastHost is not "" then
      try
        set w to window id (wid as integer)
        set t to tab (idx as integer) of w
        if (URL of t) contains lastHost then
          set URL of t to u
          set current tab of w to t
          activate
          return wid & "|" & idx & "|reused"
        end if
      end try
    end if
    if (count of windows) is 0 then
      make new document with properties {URL:u}
      set w to front window
      activate
      return ((id of w) as text) & "|1|new"
    end if
    set w to front window
    tell w to set t to make new tab with properties {URL:u}
    set current tab of w to t
    activate
    return ((id of w) as text) & "|" & ((index of t) as text) & "|new"
  end tell
end run'''

_CHROME_OPEN = '''on run argv
  set u to item 1 of argv
  set wid to item 2 of argv
  set tid to item 3 of argv
  set lastHost to item 4 of argv
  tell application "Google Chrome"
    if wid is not "" and lastHost is not "" then
      try
        set w to window id (wid as integer)
        set t to (first tab of w whose id is (tid as integer))
        if (URL of t) contains lastHost then
          set URL of t to u
          set active tab index of w to (index of t)
          activate
          return wid & "|" & tid & "|reused"
        end if
      end try
    end if
    if (count of windows) is 0 then make new window
    set w to front window
    tell w to set t to make new tab with properties {URL:u}
    set active tab index of w to (index of t)
    activate
    return ((id of w) as text) & "|" & ((id of t) as text) & "|new"
  end tell
end run'''


def _open_in_owned_tab(app: str, url: str) -> str | None:
    """Navigate JARVIS's own tab (making one if needed). Returns 'reused',
    'new', or None when this browser cannot be scripted that way."""
    import urllib.parse
    if app == "Safari":
        script = _SAFARI_OPEN
    elif app == "Google Chrome":
        script = _CHROME_OPEN
    else:
        return None
    st = _OWNED.get(app, {})
    new_host = urllib.parse.urlparse(url).netloc
    if st and _is_video_host(st.get("host", "")) and st.get("host") != new_host:
        # JARVIS opened that tab, but the user may be watching it now. Opening
        # something unrelated there would stop their film; use a new tab.
        try:
            from .media import browser_media_playing
            if browser_media_playing():
                st = {}
        except Exception:
            st = {}
    r = osa.run(script, url, str(st.get("win", "")), str(st.get("tab", "")),
                st.get("host", ""), timeout=25)
    if not r.ok or r.out.count("|") < 2:
        return None
    win, tab, mode = r.out.strip().split("|")[:3]
    _OWNED[app] = {"win": win, "tab": tab,
                   "host": urllib.parse.urlparse(url).netloc}
    return mode

def _is_video_host(host: str) -> bool:
    h = (host or "").lower()
    return any(h == v or h.endswith("." + v) for v in _VIDEO_HOSTS)


def _wait_loaded(app: str, host: str, cap: float = 8.0) -> bool:
    """Wait until the page has actually loaded, instead of a fixed sleep.

    Safari reports load state on its web area (AXLoaded / AXLoadingProgress,
    and AXURL to know it is the new page); Chrome answers `loading` over
    AppleScript. Returns as soon as the page is there - usually well under a
    second for a cached site - and gives up quietly at `cap`."""
    time.sleep(0.25)                   # let the navigation begin
    deadline = time.time() + cap
    from . import ax
    while time.time() < deadline:
        try:
            if app == "Safari" and ax.available():
                appel, _ = ax._app("Safari")
                area = ax._web_area(appel)
                if area is not None:
                    u = str(ax._attr(area, "AXURL") or "")
                    ok_host = not host or host.split(":")[0].removeprefix("www.") in u
                    # Measured on Wikipedia: the new page is committed at
                    # 1.7 s, readable at 90% (2.7 s), but only "loaded" at
                    # 6.6 s once every image and tracker finished. Readable
                    # is what matters.
                    prog = float(ax._attr(area, "AXLoadingProgress") or 0)
                    if ok_host and (ax._attr(area, "AXLoaded") is True or prog >= 0.85):
                        time.sleep(0.25)     # let scripts paint the first screen
                        return True
            elif app in CHROMIUM:
                r = osa.run(f'tell application "{app}" to return loading of active tab '
                            f'of front window', timeout=5)
                if r.ok and r.out.strip() == "false":
                    time.sleep(0.3)
                    return True
            else:
                time.sleep(min(cap, 3))
                return False
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _page_snapshot(app: str, max_chars: int = 1500) -> dict:
    """Title, URL and the start of the visible text, read through
    accessibility (works with scripting off). Saves the model a separate
    browser_read after every open or click."""
    out: dict = {}
    try:
        from . import ax
        if app == "Safari" and ax.available():
            appel, _ = ax._app("Safari")
            area = ax._web_area(appel)
            if area is not None:
                out["url"] = str(ax._attr(area, "AXURL") or "")
                out["title"] = ax._str(ax._attr(area, "AXDescription"))
        txt = ax.web_text(app, max_chars + 1) if ax.available() else None
        if txt:
            out["text"] = txt[:max_chars]
            out["more"] = len(txt) > max_chars
    except Exception:
        pass
    if not out.get("title"):
        ok, t = run_js("document.title", app)
        if ok:
            out["title"] = t
    return out


_VIDEO_HOSTS = ("netflix.com", "youtube.com", "youtu.be", "primevideo.com", "hotstar.com",
                "disneyplus.com", "twitch.tv", "vimeo.com", "jiocinema.com", "hulu.com",
                "max.com", "spotify.com", "sonyliv.com", "zee5.com")
_WATCH_MARKERS = ("/watch", "watch?v=", "youtu.be/", "/video/", "/videos/", "/play",
                  "/title/", "/episode", "/track/", "/live")


@REGISTRY.tool(
    "browser_open",
    "Open a URL in the browser and wait for it to load. Use this to go straight "
    "to a page instead of clicking through a site. Reuses JARVIS's own tab, so "
    "calling it repeatedly does not pile up tabs.",
    {"type": "object", "properties": {
        "url": {"type": "string"},
        "browser": {"type": "string", "enum": ["Safari", "Google Chrome"]},
        "wait": {"type": "number", "description": "Seconds to wait for load, default 3"}},
     "required": ["url"]},
    risk=Risk.LOW, category="read", parallel_safe=False, slow=True)
def browser_open(url: str, browser: str = "", wait: float = 3.0):
    import subprocess
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    app = browser or _active_browser() or "Safari"
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    is_video_page = any(host == h or host.endswith("." + h) for h in _VIDEO_HOSTS) \
        and any(m in url for m in _WATCH_MARKERS)
    before = None
    if is_video_page:
        from .media import browser_media_ids
        before = browser_media_ids()
    if app == "Safari" and app not in _JS_BLOCKED_AT and app not in _JS_OK:
        # Learn once whether page scripting is allowed (0.1 s), so the model is
        # never offered browser_js on a browser where it can only fail.
        ok_js, _ = run_js("1", app, timeout=5)
        if ok_js:
            _JS_OK.add(app)
    mode = _open_in_owned_tab(app, url)
    if mode is None:                       # scripting unavailable: old behaviour
        subprocess.run(["open", "-a", app, url], capture_output=True, timeout=20)
    t_load = time.time()
    loaded = _wait_loaded(app, host, cap=max(1.0, min(float(wait or 3) + 3, 15)))
    snap = _page_snapshot(app, 1800)
    content = {"url": snap.get("url") or url, "browser": app,
               "title": snap.get("title", ""), "loaded": loaded,
               "load_seconds": round(time.time() - t_load, 1)}
    if snap.get("text"):
        content["page_text"] = snap["text"]
        if snap.get("more"):
            content["note_page"] = "start of the page; browser_read for the rest"
    title = content["title"]
    ok = bool(title)
    if is_video_page:
        # A page that blocks scripting cannot be asked whether its <video> is
        # playing, but macOS knows: a playing page holds a media assertion.
        # Judged by a NEW assertion, so another tab's audio does not count.
        from .media import browser_media_ids
        playing = None
        deadline = time.time() + 6
        while time.time() < deadline:
            now = browser_media_ids()
            if now is None:
                break
            if now - (before or set()):
                playing = True
                break
            time.sleep(0.5)
        else:
            playing = False
        content["media_playing"] = playing
        if playing:
            content["verified"] = True
            content["note"] = ("the video is playing (macOS reports a new media "
                               "playback session); no need to reload or re-check")
        elif playing is False:
            content["note"] = ("no playback started yet: it may be waiting on a "
                               "Play / Resume button, a profile picker or a sign-in")
    return ToolResult(True, content=content,
                      display=f"opened {url[:56]}"
                              + (f" — {title[:40]}" if ok and title else "")
                              + (" (playing)" if content.get("media_playing") else ""))


@REGISTRY.tool(
    "browser_read",
    "Read the current web page: its title, URL and visible text. Use to see "
    "what is actually on the page before acting on it.",
    {"type": "object", "properties": {
        "selector": {"type": "string",
                     "description": "Optional CSS selector to read just part of the page"},
        "max_chars": {"type": "integer"}},
     "required": []},
    risk=Risk.SAFE, category="read", slow=True)
def browser_read(selector: str = "", max_chars: int = 6000):
    sel = json.dumps(selector or "body")
    ok, out = run_js(
        f"(function(){{var e=document.querySelector({sel});"
        f"return JSON.stringify({{t:document.title,u:location.href,"
        f"x:(e?e.innerText:'').slice(0,{int(max_chars)})}});}})()")
    if not ok:
        if _js_blocked(out):
            # Accessibility first: the whole page, including what is scrolled
            # off screen, with no setting changed.
            from . import ax
            app = _active_browser() or "Safari"
            txt = ax.web_text(app, max_chars) if ax.available() else None
            if txt:
                u = osa.run(f'on run argv\ntell application "{app}" to return URL of '
                            f'front document\nend run', timeout=10) if app == "Safari" else None
                return ToolResult(True, content={"url": u.out if u and u.ok else "",
                                                 "text": txt, "via": "accessibility"},
                                  display=f"read page ({len(txt)} chars, via accessibility)")
            # Last resort: read the pixels in view.
            from .vision_ground import ocr_screen
            items = ocr_screen(force=True)
            text = "\n".join(i["text"] for i in items)[:max_chars]
            u = osa.run('on run argv\ntell application "Safari" to return URL of '
                        'front document\nend run', timeout=10)
            return ToolResult(True, content={"url": u.out if u.ok else "",
                                             "text": text, "via": "ocr_fallback"},
                              display=f"read visible page text ({len(items)} items)")
        return ToolResult(False, error=out)
    try:
        d = json.loads(out)
    except Exception:
        return ToolResult(True, content={"text": out[:max_chars]},
                          display="read page")
    return ToolResult(True, content={"title": d.get("t"), "url": d.get("u"),
                                     "text": d.get("x", "")},
                      display=f"read {(d.get('t') or '')[:50]} "
                              f"({len(d.get('x',''))} chars)")


@REGISTRY.tool(
    "browser_find",
    "List the clickable things on the current page — links, buttons, inputs — "
    "with the exact text to pass to browser_click. Use this instead of guessing "
    "what is on the page.",
    {"type": "object", "properties": {
        "contains": {"type": "string", "description": "Only items containing this text"},
        "limit": {"type": "integer"}},
     "required": []},
    risk=Risk.SAFE, category="read", slow=True)
def browser_find(contains: str = "", limit: int = 40):
    needle = json.dumps((contains or "").lower())
    js = f"""(function(){{
      var q=document.querySelectorAll('a,button,[role=button],[role=link],input,select,textarea,[onclick]');
      var out=[],n={int(limit)},needle={needle};
      for (var i=0;i<q.length && out.length<n;i++){{
        var e=q[i], r=e.getBoundingClientRect();
        if(r.width<2||r.height<2) continue;
        var st=window.getComputedStyle(e);
        if(st.visibility==='hidden'||st.display==='none'||st.opacity==='0') continue;
        var t=(e.innerText||e.value||e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.title||'').trim().replace(/\\s+/g,' ').slice(0,80);
        if(!t) continue;
        if(needle && t.toLowerCase().indexOf(needle)<0) continue;
        out.push({{tag:e.tagName.toLowerCase(), text:t,
                   href:(e.getAttribute('href')||'').slice(0,120)}});
      }}
      return JSON.stringify(out);
    }})()"""
    ok, out = run_js(js)
    if not ok:
        if _js_blocked(out):
            from . import ax
            ctl = ax.web_controls(_active_browser() or "Safari", contains, limit) \
                if ax.available() else None
            if ctl:
                return ToolResult(True, content={"clickable": ctl, "count": len(ctl),
                                                 "via": "accessibility"},
                                  display=f"{len(ctl)} control(s) on the page")
            from .vision_ground import ocr_screen
            items = [{"tag": "text", "text": i["text"], "x": i["x"], "y": i["y"]}
                     for i in ocr_screen(force=True)
                     if not contains or contains.lower() in i["text"].lower()]
            return ToolResult(True, content={"clickable": items[:limit],
                                             "count": len(items),
                                             "via": "ocr_fallback"},
                              display=f"{len(items)} visible text item(s) "
                                      f"(OCR; click with click_text)")
        return ToolResult(False, error=out)
    try:
        items = json.loads(out or "[]")
    except Exception:
        items = []
    return ToolResult(True, content={"clickable": items, "count": len(items)},
                      display=f"{len(items)} clickable item(s)"
                              + (f" matching {contains!r}" if contains else ""))


def _web_click_verdict(args: dict) -> Verdict:
    return Verdict(Risk.MEDIUM, "clicks an element on the current web page",
                   category="ui_input", needs_confirm=False)


def _attach_page_after(r: ToolResult, app: str) -> None:
    """After a click, wait for any navigation and show the new page start."""
    try:
        _wait_loaded(app, "", cap=4.0)
        snap = _page_snapshot(app, 1000)
        if isinstance(r.content, dict):
            r.content["page_after"] = {k: snap[k] for k in ("url", "title", "text") if snap.get(k)}
    except Exception:
        pass


@REGISTRY.tool(
    "browser_click",
    "Click a link or button on the current page by its visible text. Matches "
    "through the DOM, so it is exact — no coordinates involved.",
    {"type": "object", "properties": {
        "text": {"type": "string", "description": "Visible text of the link or button"},
        "exact": {"type": "boolean", "description": "Require an exact match"},
        "index": {"type": "integer", "description": "Which match, 1-based"}},
     "required": ["text"]},
    risk=Risk.MEDIUM, category="ui_input", assess=_web_click_verdict,
    parallel_safe=False, slow=True)
def browser_click(text: str, exact: bool = False, index: int = 1):
    needle = json.dumps(text.lower())
    js = f"""(function(){{
      var q=document.querySelectorAll('a,button,[role=button],[role=link],[onclick],input[type=submit],input[type=button]');
      var hits=[],needle={needle},exact={str(bool(exact)).lower()};
      for (var i=0;i<q.length;i++){{
        var e=q[i], r=e.getBoundingClientRect();
        if(r.width<2||r.height<2) continue;
        var st=window.getComputedStyle(e);
        if(st.visibility==='hidden'||st.display==='none') continue;
        var t=(e.innerText||e.value||e.getAttribute('aria-label')||e.title||'').trim().replace(/\\s+/g,' ');
        var lt=t.toLowerCase();
        if(exact ? lt===needle : lt.indexOf(needle)>=0) hits.push([e,t]);
      }}
      if(!hits.length) return JSON.stringify({{ok:false,n:0}});
      var pick=hits[{max(1, index)}-1]; if(!pick) return JSON.stringify({{ok:false,n:hits.length}});
      pick[0].scrollIntoView({{block:'center'}});
      pick[0].click();
      return JSON.stringify({{ok:true,n:hits.length,text:pick[1].slice(0,80)}});
    }})()"""
    ok, out = run_js(js)
    if not ok:
        if _js_blocked(out):
            from . import ax
            if ax.available():
                app_ = _active_browser() or "Safari"
                r = ax.press_web(app_, text, max(1, index))
                if r.ok:
                    _attach_page_after(r, app_)
                    return r
            from .vision_ground import click_text as _ocr_click
            r = _ocr_click(text=text, occurrence=max(1, index))
            if r.ok:
                r.content["via"] = "ocr_fallback"
                r.display += "  (via on-screen text; enable JS-from-Apple-Events "
                r.display += "for exact DOM clicks)"
            return r
        return ToolResult(False, error=out)
    try:
        d = json.loads(out or "{}")
    except Exception:
        d = {}
    if not d.get("ok"):
        return ToolResult(False, error=(
            f"No clickable element matching {text!r} on this page"
            + (f" (found {d.get('n')} but index {index} is out of range)"
               if d.get("n") else "")
            + ". Use browser_find to see what is actually clickable."))
    time.sleep(0.6)
    return ToolResult(True, content={"clicked": d.get("text"),
                                     "matches": d.get("n")},
                      display=f"clicked {str(d.get('text'))[:44]!r} on the page")


@REGISTRY.tool(
    "browser_type",
    "Type into a field on the current page, found by its placeholder, label or "
    "name. Optionally press Enter to submit.",
    {"type": "object", "properties": {
        "field": {"type": "string",
                  "description": "Placeholder, aria-label, name or id of the field"},
        "text": {"type": "string"},
        "submit": {"type": "boolean"}},
     "required": ["field", "text"]},
    risk=Risk.MEDIUM, category="ui_input", assess=_web_click_verdict,
    parallel_safe=False, slow=True)
def browser_type(field: str, text: str, submit: bool = False):
    js = f"""(function(){{
      var needle={json.dumps(field.lower())}, val={json.dumps(text)};
      var q=document.querySelectorAll('input,textarea,[contenteditable=true]');
      var el=null;
      for (var i=0;i<q.length;i++){{
        var e=q[i], r=e.getBoundingClientRect();
        if(r.width<2||r.height<2) continue;
        var d=((e.getAttribute('placeholder')||'')+' '+(e.getAttribute('aria-label')||'')+' '+(e.getAttribute('name')||'')+' '+(e.id||'')+' '+(e.type||'')).toLowerCase();
        if(d.indexOf(needle)>=0){{el=e;break;}}
      }}
      if(!el){{ for (var i=0;i<q.length;i++){{ var r=q[i].getBoundingClientRect();
                 if(r.width>40&&r.height>10){{el=q[i];break;}} }} }}
      if(!el) return JSON.stringify({{ok:false}});
      el.focus();
      if(el.isContentEditable){{ el.innerText=val; }}
      else {{ var s=Object.getOwnPropertyDescriptor(el.constructor.prototype,'value');
              if(s&&s.set) s.set.call(el,val); else el.value=val; }}
      el.dispatchEvent(new Event('input',{{bubbles:true}}));
      el.dispatchEvent(new Event('change',{{bubbles:true}}));
      if({str(bool(submit)).lower()}){{
        el.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',keyCode:13,which:13,bubbles:true}}));
        if(el.form) {{ try{{el.form.requestSubmit?el.form.requestSubmit():el.form.submit();}}catch(e){{}} }}
      }}
      return JSON.stringify({{ok:true}});
    }})()"""
    ok, out = run_js(js)
    if not ok:
        if _js_blocked(out):
            from . import ax
            if ax.available():
                r = ax.set_web_field(_active_browser() or "Safari", field, text)
                if r.ok:
                    if submit:
                        from .control import press_key
                        press_key(keys="return", app=_active_browser() or "Safari", quiet=True)
                        r.content["submitted"] = True
                    return r
            from .vision_ground import type_into as _ocr_type
            return _ocr_type(field=field, text=text, submit=submit)
        return ToolResult(False, error=out)
    try:
        d = json.loads(out or "{}")
    except Exception:
        d = {}
    if not d.get("ok"):
        return ToolResult(False, error=(
            f"No input field matching {field!r}. Use browser_find to see the "
            f"fields on the page."))
    if submit:
        time.sleep(1.2)
    return ToolResult(True, content={"field": field, "typed": len(text),
                                     "submitted": bool(submit)},
                      display=f"typed into {field!r}"
                              + (" and submitted" if submit else ""))


_EDITOR_JS = r"""(function(code){
  function done(kind, v){ return JSON.stringify({ok:true, kind:kind, value:v}); }
  try { if (window.monaco && monaco.editor) {
      var eds = monaco.editor.getEditors ? monaco.editor.getEditors() : [];
      var m = eds.length ? eds[0].getModel() : monaco.editor.getModels()[0];
      if (m) { m.setValue(code); return done('monaco', m.getValue()); } } } catch(e) {}
  try { var c6 = document.querySelector('.cm-content');
      if (c6 && c6.cmView) { var v = c6.cmView.view;
        v.dispatch({changes:{from:0, to:v.state.doc.length, insert:code}});
        return done('codemirror6', v.state.doc.toString()); } } catch(e) {}
  try { var c5 = document.querySelector('.CodeMirror');
      if (c5 && c5.CodeMirror) { c5.CodeMirror.setValue(code);
        return done('codemirror5', c5.CodeMirror.getValue()); } } catch(e) {}
  try { var ae = document.querySelector('.ace_editor');
      if (ae && window.ace) { var ed = ace.edit(ae); ed.setValue(code, 1);
        return done('ace', ed.getValue()); } } catch(e) {}
  var ta = document.querySelector('textarea');
  if (ta) { var s = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
    s.call(ta, code); ta.dispatchEvent(new Event('input', {bubbles:true}));
    return done('textarea', ta.value); }
  return JSON.stringify({ok:false});
})(%s)"""


def _norm_code(s: str) -> str:
    return "\n".join(l.rstrip() for l in (s or "").replace("\r\n", "\n").strip().split("\n"))


def _flat(s: str) -> str:
    """Whitespace-insensitive form: editors render spaces as U+00A0 and may
    re-indent, neither of which changes the code."""
    return " ".join((s or "").replace("\u00a0", " ").split())


def _pasted_code_seen(code: str, editor_value: str, page_text: str) -> bool:
    """Did a paste of `code` land in the page's editor?

    Monaco (LeetCode) keeps only a window of lines around the cursor in its
    text area and hides its rendered lines from accessibility, so the old check
    - first line of the code somewhere in the page text - failed on 15 of 22
    real pastes that had worked (after a paste the cursor, and so the window,
    is at the END of the code). The editor's own value is checked first: its
    window must hold the code's last line and nothing that is not in the code.
    """
    lines = [l for l in (code or "").splitlines() if l.strip()]
    if not lines:
        return False
    flat_code = _flat(code)
    last = _flat(lines[-1])
    win = _flat(editor_value)
    if win and len(win) >= min(8, len(flat_code)) and last in win and win in flat_code:
        return True
    page = _flat(page_text)
    if not page:
        return False
    # Only a distinctive line near the end counts: "}" is on every code page,
    # and the first line ("class Solution:") is usually the site's own
    # template, which is there whether or not the paste landed.
    probe = next((_flat(l) for l in reversed(lines) if len(_flat(l)) >= 12), "")
    return bool(probe) and probe in page


@REGISTRY.tool(
    "browser_set_editor",
    "Replace the contents of the code editor on the current web page (LeetCode, "
    "HackerRank, online IDEs - Monaco, CodeMirror, Ace or a plain text box) "
    "with exactly this code, and read it back to confirm. Use this instead of "
    "typing code: typing makes editors auto-indent and auto-close brackets.",
    {"type": "object", "properties": {"code": {"type": "string"}},
     "required": ["code"]},
    risk=Risk.MEDIUM, category="ui_input", parallel_safe=False)
def browser_set_editor(code: str):
    ok, out = run_js(_EDITOR_JS % json.dumps(code))
    if ok:
        try:
            d = json.loads(out or "{}")
        except Exception:
            d = {}
        if d.get("ok"):
            same = _norm_code(d.get("value", "")) == _norm_code(code)
            if same:
                return ToolResult(True, content={"editor": d["kind"], "verified": True,
                                                 "lines": code.count("\n") + 1},
                                  display=f"set {d['kind']} editor "
                                          f"({code.count(chr(10)) + 1} lines, verified)")
            return ToolResult(False, error=f"set the {d['kind']} editor but it now "
                                           f"holds different text - the page may "
                                           f"reformat on input")
        return ToolResult(False, error="no code editor found on this page")
    if not _js_blocked(out):
        return ToolResult(False, error=out)
    # No scripting allowed: focus the editor through accessibility and paste,
    # which editors take verbatim (no auto-indent, no bracket pairing).
    from . import ax
    from .control import focus_app, paste_text
    app = _active_browser() or "Safari"
    ed = ax.web_editor(app) if ax.available() else None
    if ed is None:
        return ToolResult(False, error=(
            "could not find the page's code editor without JavaScript. " + out))
    ok_f, msg = focus_app(app)
    if not ok_f:
        return ToolResult(False, error=msg)
    ax._ax().AXUIElementSetAttributeValue(ed, "AXFocused", True)
    time.sleep(0.2)
    r = paste_text(text=code, app=app, replace=True)
    if not r.ok:
        return r
    time.sleep(0.4)
    try:
        value = ax._str(ax._attr(ed, "AXValue"))
    except Exception:
        value = ""
    seen = _pasted_code_seen(code, value, "")
    if not seen:
        seen = _pasted_code_seen(code, "", ax.web_text(app, 20000) or "")
    return ToolResult(seen, content={"editor": "pasted", "verified": seen},
                      display=f"pasted {code.count(chr(10)) + 1} lines into the editor",
                      error=None if seen else
                      "pasted, but the editor does not show the code afterwards - "
                      "check with see_screen before submitting")


@REGISTRY.tool(
    "browser_select",
    "Choose an option from a dropdown or menu on a web page - including custom "
    "ones like LeetCode's editor language picker. Give the dropdown's current "
    "visible label (e.g. 'Python3') and the option to pick (e.g. 'Java'). It "
    "confirms the dropdown now shows the option.",
    {"type": "object", "properties": {
        "control": {"type": "string", "description": "The dropdown's current label"},
        "option": {"type": "string"}},
     "required": ["control", "option"]},
    risk=Risk.MEDIUM, category="ui_input", parallel_safe=False)
def browser_select(control: str, option: str):
    from . import ax
    return ax.select_web_option(_active_browser() or "Safari", control, option)


@REGISTRY.tool(
    "browser_js",
    "Run JavaScript in the current tab and return the result. The escape hatch "
    "for anything the other browser tools do not cover — pressing a player's "
    "play button, reading structured data, scrolling a specific container.",
    {"type": "object", "properties": {
        "code": {"type": "string",
                 "description": "JavaScript expression; its value is returned"}},
     "required": ["code"]},
    risk=Risk.MEDIUM, category="ui_input", parallel_safe=False, slow=True,
    assess=lambda a: Verdict(Risk.MEDIUM,
                             "runs JavaScript in the page you have open",
                             category="ui_input", needs_confirm=False))
def browser_js(code: str):
    ok, out = run_js(code)
    if not ok:
        if _js_blocked(out):
            # Once blocking is known run_js answers with just the marker phrase;
            # 17 of 17 real calls ended here, and the model kept trying new
            # scripts. Say what to do instead - the other tools work without it.
            app = _active_browser() or "Safari"
            out = (f"{app} is blocking scripted JavaScript (the user's setting: "
                   + ("Safari > Settings > Advanced > 'Show features for web "
                      "developers', then Develop > 'Allow JavaScript from Apple "
                      "Events'" if app == "Safari" else
                      f"{app} > View > Developer > 'Allow JavaScript from Apple "
                      f"Events'")
                   + "). Do not retry browser_js. browser_read, browser_find, "
                   "browser_click, browser_type, browser_select and "
                   "browser_set_editor all work without it; video_state for "
                   "playback.")
        return ToolResult(False, error=out)
    return ToolResult(True, content={"result": out[:8000]},
                      display=f"ran JS → {out[:60]}" if out else "ran JS")
