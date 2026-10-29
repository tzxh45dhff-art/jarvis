"""
Calendar, Reminders and Notes.

Backed by EventKit rather than AppleScript: a seven-day event query takes about
one millisecond through EventKit and about nine seconds through AppleScript,
which is the difference between "check my calendar" feeling instant and feeling
broken. AppleScript remains the fallback when EventKit access is unavailable.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading

from ..safety.policy import Risk, Verdict
from . import osa
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.pim")

_EK = None
_EK_STATE = {"events": None, "reminders": None}
_LOCK = threading.Lock()


def _store():
    global _EK
    with _LOCK:
        if _EK is None:
            import EventKit as EK
            _EK = EK.EKEventStore.alloc().init()
        return _EK


def _grant(kind: str) -> bool:
    """Request EventKit access; remember only a grant. A refusal used to be
    cached for the life of the engine, so approving access afterwards did
    nothing until a restart."""
    if _EK_STATE[kind]:
        return True
    try:
        st = _store()
    except Exception as e:
        log.warning("EventKit unavailable: %s", e)
        return False
    done = threading.Event()
    out = {}

    def cb(granted, err):
        out["g"] = bool(granted)
        done.set()

    try:
        if kind == "events" and hasattr(st, "requestFullAccessToEventsWithCompletion_"):
            st.requestFullAccessToEventsWithCompletion_(cb)
        elif kind == "reminders" and hasattr(st, "requestFullAccessToRemindersWithCompletion_"):
            st.requestFullAccessToRemindersWithCompletion_(cb)
        else:
            st.requestAccessToEntityType_completion_(0 if kind == "events" else 1, cb)
        done.wait(45)
    except Exception as e:
        log.warning("EventKit request failed: %s", e)
    _EK_STATE[kind] = bool(out.get("g")) or None
    return bool(_EK_STATE[kind])


# --- natural dates -----------------------------------------------------------
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]


def parse_when(text: str, base: dt.datetime | None = None
               ) -> tuple[dt.datetime, dt.datetime]:
    """Turn 'tomorrow', 'this week', 'next friday', '2026-09-10' into a range."""
    now = base or dt.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t = (text or "today").strip().lower()

    if not t or t in ("today",):
        return midnight, midnight + dt.timedelta(days=1)
    if t == "now":
        return now, now + dt.timedelta(hours=4)
    if t == "tomorrow":
        d = midnight + dt.timedelta(days=1)
        return d, d + dt.timedelta(days=1)
    if t == "yesterday":
        d = midnight - dt.timedelta(days=1)
        return d, d + dt.timedelta(days=1)
    if t in ("this week", "week"):
        start = midnight - dt.timedelta(days=midnight.weekday())
        return start, start + dt.timedelta(days=7)
    if t == "next week":
        start = midnight - dt.timedelta(days=midnight.weekday()) + dt.timedelta(days=7)
        return start, start + dt.timedelta(days=7)
    if t in ("this weekend", "weekend"):
        sat = midnight + dt.timedelta(days=(5 - midnight.weekday()) % 7)
        return sat, sat + dt.timedelta(days=2)
    if t in ("this month", "month"):
        start = midnight.replace(day=1)
        nxt = (start + dt.timedelta(days=32)).replace(day=1)
        return start, nxt
    m = re.match(r"^(?:in\s+)?(?:the\s+)?next\s+(\d+)\s+(day|week|hour)s?$", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"day": dt.timedelta(days=n), "week": dt.timedelta(weeks=n),
                 "hour": dt.timedelta(hours=n)}[unit]
        return now, now + delta
    m = re.match(r"^(?:next\s+)?(" + "|".join(_WEEKDAYS) + r")$", t)
    if m:
        target = _WEEKDAYS.index(m.group(1))
        ahead = (target - midnight.weekday()) % 7
        if ahead == 0 or t.startswith("next"):
            ahead = ahead or 7
        d = midnight + dt.timedelta(days=ahead)
        return d, d + dt.timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %B %Y", "%B %d %Y"):
        try:
            d = dt.datetime.strptime(text.strip(), fmt)
            return d, d + dt.timedelta(days=1)
        except ValueError:
            continue
    # default: the coming week
    return midnight, midnight + dt.timedelta(days=7)


def _nsdate(d: dt.datetime):
    from Foundation import NSDate
    return NSDate.dateWithTimeIntervalSince1970_(d.timestamp())


def _pydate(nsd) -> dt.datetime | None:
    if nsd is None:
        return None
    try:
        return dt.datetime.fromtimestamp(nsd.timeIntervalSince1970())
    except Exception:
        return None


def _events(start: dt.datetime, end: dt.datetime) -> list[dict]:
    if not _grant("events"):
        raise PermissionError(
            "Calendar access is not granted. macOS asks for it the first time; "
            "approve it, or enable it under System Settings > Privacy & Security "
            "> Calendars for the app running JARVIS.")
    st = _store()
    pred = st.predicateForEventsWithStartDate_endDate_calendars_(
        _nsdate(start), _nsdate(end), None)
    out = []
    for e in st.eventsMatchingPredicate_(pred) or []:
        s, t = _pydate(e.startDate()), _pydate(e.endDate())
        out.append({
            "id": str(e.eventIdentifier() or ""),
            "title": e.title() or "(untitled)",
            "start": s.isoformat(timespec="minutes") if s else None,
            "end": t.isoformat(timespec="minutes") if t else None,
            "all_day": bool(e.isAllDay()),
            "location": e.location() or "",
            "calendar": e.calendar().title() if e.calendar() else "",
            "notes": (e.notes() or "")[:300],
        })
    out.sort(key=lambda x: x["start"] or "")
    return out


@REGISTRY.tool(
    "check_calendar",
    "Read calendar events for a time range. Use this whenever the user asks "
    "about their schedule, whether they are free or busy, what is coming up, or "
    "anything time-dependent. Accepts natural ranges like 'today', 'tomorrow', "
    "'this week', 'next friday'.",
    {"type": "object", "properties": {
        "when": {"type": "string",
                 "description": "today | tomorrow | this week | next week | "
                                "weekend | next 3 days | monday | YYYY-MM-DD"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def check_calendar(when: str = "today"):
    start, end = parse_when(when)
    try:
        evs = _events(start, end)
    except PermissionError as e:
        return ToolResult(False, error=str(e))
    except Exception as e:
        return ToolResult(False, error=f"calendar read failed: {e}")
    return ToolResult(True, content={
        "range": when,
        "from": start.isoformat(timespec="minutes"),
        "to": end.isoformat(timespec="minutes"),
        "count": len(evs), "events": evs},
        display=f"{len(evs)} event(s) {when}")


@REGISTRY.tool(
    "check_availability",
    "Work out whether the user is free during a period, returning their busy "
    "blocks and the gaps between them. Use for 'am I free tomorrow?', "
    "'when can I fit in an hour?', 'is my afternoon clear?'.",
    {"type": "object", "properties": {
        "when": {"type": "string", "description": "today | tomorrow | monday | ..."},
        "day_start": {"type": "integer", "description": "Working day start hour, default 9"},
        "day_end": {"type": "integer", "description": "Working day end hour, default 21"},
        "min_gap_minutes": {"type": "integer", "description": "Smallest useful gap, default 30"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def check_availability(when: str = "tomorrow", day_start: int = 9,
                       day_end: int = 21, min_gap_minutes: int = 30):
    start, end = parse_when(when)
    try:
        evs = _events(start, end)
    except PermissionError as e:
        return ToolResult(False, error=str(e))
    except Exception as e:
        return ToolResult(False, error=f"calendar read failed: {e}")

    day = start.replace(hour=day_start, minute=0)
    day_close = start.replace(hour=day_end, minute=0)
    busy = []
    all_day = []
    for e in evs:
        if e["all_day"]:
            all_day.append(e["title"])
            continue
        try:
            s = dt.datetime.fromisoformat(e["start"])
            t = dt.datetime.fromisoformat(e["end"])
        except (TypeError, ValueError):
            continue
        busy.append((max(s, day), min(t, day_close), e["title"]))
    busy.sort()

    free = []
    cursor = day
    for s, t, _ in busy:
        if s > cursor:
            gap = (s - cursor).total_seconds() / 60
            if gap >= min_gap_minutes:
                free.append({"from": cursor.strftime("%H:%M"),
                             "to": s.strftime("%H:%M"), "minutes": int(gap)})
        cursor = max(cursor, t)
    if cursor < day_close:
        gap = (day_close - cursor).total_seconds() / 60
        if gap >= min_gap_minutes:
            free.append({"from": cursor.strftime("%H:%M"),
                         "to": day_close.strftime("%H:%M"), "minutes": int(gap)})

    total_busy = sum((t - s).total_seconds() for s, t, _ in busy) / 3600
    return ToolResult(True, content={
        "range": when,
        "date": start.strftime("%A %d %B %Y"),
        "all_day_events": all_day,
        "busy": [{"from": s.strftime("%H:%M"), "to": t.strftime("%H:%M"),
                  "title": n} for s, t, n in busy],
        "free_slots": free,
        "busy_hours": round(total_busy, 1),
        "is_clear": not busy and not all_day},
        display=(f"{when}: {len(busy)} commitment(s), "
                 f"{len(free)} free slot(s)"))


@REGISTRY.tool(
    "create_event",
    "Add an event to the calendar.",
    {"type": "object", "properties": {
        "title": {"type": "string"},
        "start": {"type": "string", "description": "ISO datetime, e.g. 2026-09-08T14:00"},
        "end": {"type": "string", "description": "ISO datetime; defaults to one hour later"},
        "location": {"type": "string"},
        "notes": {"type": "string"},
        "calendar": {"type": "string", "description": "Calendar name; defaults to the default one"}},
     "required": ["title", "start"]},
    risk=Risk.MEDIUM, category="calendar_write", parallel_safe=False,
    assess=lambda a: Verdict(Risk.MEDIUM, "adds an event to your calendar",
                             category="calendar_write", needs_confirm=False))
def create_event(title: str, start: str, end: str = "", location: str = "",
                 notes: str = "", calendar: str = ""):
    if not _grant("events"):
        return ToolResult(False, error="Calendar access is not granted.")
    try:
        s = dt.datetime.fromisoformat(start)
    except ValueError:
        return ToolResult(False, error=f"Could not parse start time {start!r}; "
                                       f"use ISO format like 2026-09-08T14:00")
    try:
        e = dt.datetime.fromisoformat(end) if end else s + dt.timedelta(hours=1)
    except ValueError:
        e = s + dt.timedelta(hours=1)

    import EventKit as EK
    st = _store()
    ev = EK.EKEvent.eventWithEventStore_(st)
    ev.setTitle_(title)
    ev.setStartDate_(_nsdate(s))
    ev.setEndDate_(_nsdate(e))
    if location:
        ev.setLocation_(location)
    if notes:
        ev.setNotes_(notes)
    cal = None
    if calendar:
        for c in st.calendarsForEntityType_(0) or []:
            if c.title().lower() == calendar.lower():
                cal = c
                break
    ev.setCalendar_(cal or st.defaultCalendarForNewEvents())
    ok, err = st.saveEvent_span_error_(ev, 0, None)
    if not ok:
        return ToolResult(False, error=f"could not save event: {err}")
    return ToolResult(True, content={
        "title": title, "start": s.isoformat(timespec="minutes"),
        "end": e.isoformat(timespec="minutes"),
        "calendar": ev.calendar().title() if ev.calendar() else ""},
        display=f"added '{title}' on {s.strftime('%a %d %b %H:%M')}")


def _fetch_reminders(completed: bool = False) -> list[dict]:
    if not _grant("reminders"):
        raise PermissionError(
            "Reminders access is not granted. Approve the macOS prompt, or "
            "enable it under System Settings > Privacy & Security > Reminders.")
    st = _store()
    cals = st.calendarsForEntityType_(1)
    pred = (st.predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
                None, None, cals) if completed else
            st.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                None, None, cals))
    done = threading.Event()
    box = {}

    def cb(items):
        box["items"] = items
        done.set()

    st.fetchRemindersMatchingPredicate_completion_(pred, cb)
    done.wait(25)
    out = []
    for r in box.get("items") or []:
        due = None
        try:
            comps = r.dueDateComponents()
            if comps and comps.date():
                due = _pydate(comps.date())
        except Exception:
            pass
        out.append({
            "title": r.title() or "",
            "due": due.isoformat(timespec="minutes") if due else None,
            "list": r.calendar().title() if r.calendar() else "",
            "priority": int(r.priority() or 0),
            "notes": (r.notes() or "")[:200],
            "completed": bool(r.isCompleted()),
        })
    out.sort(key=lambda x: (x["due"] is None, x["due"] or ""))
    return out


@REGISTRY.tool(
    "check_reminders",
    "Read the user's reminders / to-do items. Use alongside check_calendar when "
    "answering questions about what they need to do, whether they are free, or "
    "what is outstanding.",
    {"type": "object", "properties": {
        "when": {"type": "string",
                 "description": "Optional filter: today, tomorrow, this week, overdue"},
        "include_completed": {"type": "boolean"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def check_reminders(when: str = "", include_completed: bool = False):
    try:
        items = _fetch_reminders(completed=include_completed)
    except PermissionError as e:
        return ToolResult(False, error=str(e))
    except Exception as e:
        return ToolResult(False, error=f"reminders read failed: {e}")

    now = dt.datetime.now()
    filtered = items
    if when:
        w = when.strip().lower()
        if w == "overdue":
            filtered = [i for i in items
                        if i["due"] and dt.datetime.fromisoformat(i["due"]) < now
                        and not i["completed"]]
        else:
            start, end = parse_when(w)
            filtered = [i for i in items if i["due"]
                        and start <= dt.datetime.fromisoformat(i["due"]) < end]
    overdue = [i for i in items
               if i["due"] and dt.datetime.fromisoformat(i["due"]) < now
               and not i["completed"]]
    return ToolResult(True, content={
        "filter": when or "all open",
        "count": len(filtered),
        "reminders": filtered[:60],
        "overdue_count": len(overdue)},
        display=f"{len(filtered)} reminder(s)"
                + (f", {len(overdue)} overdue" if overdue else ""))


@REGISTRY.tool(
    "create_reminder",
    "Add a reminder, optionally with a due date and time. Use for "
    "'remind me to ...' requests that need an alert, as opposed to remembering "
    "a fact.",
    {"type": "object", "properties": {
        "title": {"type": "string"},
        "due": {"type": "string",
                "description": "ISO datetime, e.g. 2026-09-08T09:00"},
        "notes": {"type": "string"},
        "list": {"type": "string", "description": "Reminders list name"}},
     "required": ["title"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False)
def create_reminder(title: str, due: str = "", notes: str = "", list: str = ""):
    if not _grant("reminders"):
        return ToolResult(False, error="Reminders access is not granted.")
    import EventKit as EK
    from Foundation import NSCalendar
    st = _store()
    rem = EK.EKReminder.reminderWithEventStore_(st)
    rem.setTitle_(title)
    if notes:
        rem.setNotes_(notes)
    cal = None
    if list:
        for c in st.calendarsForEntityType_(1) or []:
            if c.title().lower() == list.lower():
                cal = c
                break
    rem.setCalendar_(cal or st.defaultCalendarForNewReminders())
    when = None
    if due:
        try:
            when = dt.datetime.fromisoformat(due)
        except ValueError:
            s, _ = parse_when(due)
            when = s.replace(hour=9)
    if when:
        c = NSCalendar.currentCalendar()
        units = (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7)
        comps = c.components_fromDate_(units, _nsdate(when))
        rem.setDueDateComponents_(comps)
        alarm = EK.EKAlarm.alarmWithAbsoluteDate_(_nsdate(when))
        rem.addAlarm_(alarm)
    ok, err = st.saveReminder_commit_error_(rem, True, None)
    if not ok:
        return ToolResult(False, error=f"could not save reminder: {err}")
    return ToolResult(True, content={
        "title": title,
        "due": when.isoformat(timespec="minutes") if when else None,
        "list": rem.calendar().title() if rem.calendar() else ""},
        display=f"reminder '{title}'"
                + (f" for {when.strftime('%a %d %b %H:%M')}" if when else ""))


@REGISTRY.tool(
    "complete_reminder",
    "Mark a reminder as done, matched by its title.",
    {"type": "object", "properties": {"title": {"type": "string"}},
     "required": ["title"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False)
def complete_reminder(title: str):
    if not _grant("reminders"):
        return ToolResult(False, error="Reminders access is not granted.")
    st = _store()
    pred = st.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
        None, None, st.calendarsForEntityType_(1))
    done = threading.Event()
    box = {}

    def _cb(items):
        # must return None: PyObjC types this handler as void
        box["i"] = items
        done.set()

    st.fetchRemindersMatchingPredicate_completion_(pred, _cb)
    done.wait(25)
    q = title.strip().lower()
    for r in box.get("i") or []:
        if q in (r.title() or "").lower():
            r.setCompleted_(True)
            ok, err = st.saveReminder_commit_error_(r, True, None)
            if not ok:
                return ToolResult(False, error=str(err))
            return ToolResult(True, content={"completed": r.title()},
                              display=f"completed '{r.title()}'")
    return ToolResult(False, error=f"No open reminder matching {title!r}")


@REGISTRY.tool(
    "create_note",
    "Create a note in Apple Notes.",
    {"type": "object", "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "folder": {"type": "string"}},
     "required": ["title", "body"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False)
def create_note(title: str, body: str, folder: str = ""):
    html = ("<h1>" + title.replace("<", "&lt;") + "</h1>"
            + "".join(f"<div>{l.replace('<', '&lt;')}</div>"
                      for l in body.splitlines()))
    script = '''on run argv
  set theBody to item 1 of argv
  set fld to item 2 of argv
  tell application "Notes"
    if fld is not "" and (exists folder fld) then
      make new note at folder fld with properties {body:theBody}
    else
      make new note with properties {body:theBody}
    end if
    return "ok"
  end tell
end run'''
    r = osa.run(script, html, folder, timeout=30)
    if not r.ok:
        return ToolResult(False, error=r.error)
    return ToolResult(True, content={"title": title, "chars": len(body)},
                      display=f"created note '{title}'")


@REGISTRY.tool(
    "search_notes",
    "Search Apple Notes by text and return matching note titles and previews.",
    {"type": "object", "properties": {
        "query": {"type": "string"}, "limit": {"type": "integer"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="read", slow=True)
def search_notes(query: str, limit: int = 8):
    script = f'''on run argv
  set q to item 1 of argv
  set lim to (item 2 of argv) as integer
  set out to ""
  tell application "Notes"
    set hits to (every note whose name contains q or body contains q)
    set n to count of hits
    if n > lim then set n to lim
    repeat with i from 1 to n
      set nt to item i of hits
      set out to out & (name of nt) & "{osa.FS}" & (modification date of nt as text) & "{osa.RS}"
    end repeat
  end tell
  return out
end run'''
    r = osa.run(script, query, str(limit), timeout=45)
    if not r.ok:
        return ToolResult(False, error=r.error)
    rows = osa.parse_records(r.out, ["title", "modified"])
    return ToolResult(True, content={"query": query, "notes": rows},
                      display=f"{len(rows)} note(s) matching {query!r}")


def _find_events(title: str, when: str = "") -> list:
    """Locate EKEvent objects by fuzzy title within a window."""
    import difflib
    if not _grant("events"):
        raise PermissionError("Calendar access is not granted.")
    st = _store()
    if when:
        start, end = parse_when(when)
    else:
        # No window given: look a month either side rather than guessing.
        now = dt.datetime.now()
        start, end = now - dt.timedelta(days=31), now + dt.timedelta(days=93)
    pred = st.predicateForEventsWithStartDate_endDate_calendars_(
        _nsdate(start), _nsdate(end), None)
    q = (title or "").strip().lower()
    scored = []
    for e in st.eventsMatchingPredicate_(pred) or []:
        t = (e.title() or "").lower()
        if not t:
            continue
        if q == t:
            s = 1.0
        elif q and q in t:
            s = 0.9
        elif q:
            s = difflib.SequenceMatcher(None, q, t).ratio()
        else:
            s = 0.5
        if s >= 0.6:
            scored.append((s, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored]


@REGISTRY.tool(
    "delete_event",
    "Delete a calendar event, found by its title. Say which day if you know it. "
    "The event's details are recorded first so it can be restored with undo.",
    {"type": "object", "properties": {
        "title": {"type": "string", "description": "Event title, or part of it"},
        "when": {"type": "string",
                 "description": "Day or range to search, e.g. today, tomorrow, this week"},
        "all_occurrences": {"type": "boolean",
                            "description": "For repeating events, delete the whole series"}},
     "required": ["title"]},
    risk=Risk.MEDIUM, category="calendar_write", parallel_safe=False,
    assess=lambda a: Verdict(Risk.MEDIUM,
                             f"deletes the calendar event {a.get('title')!r} "
                             f"(recorded so it can be restored)",
                             category="calendar_write", needs_confirm=False))
def delete_event(title: str, when: str = "", all_occurrences: bool = False):
    from ..safety import journal
    try:
        matches = _find_events(title, when)
    except PermissionError as e:
        return ToolResult(False, error=str(e))
    except Exception as e:
        return ToolResult(False, error=f"calendar search failed: {e}")
    if not matches:
        return ToolResult(False, error=(
            f"No event matching {title!r}"
            + (f" {when}" if when else " in the next three months")
            + ". Check the exact title with check_calendar."))
    if len(matches) > 1:
        titles = [f"{e.title()} on "
                  f"{(_pydate(e.startDate()) or dt.datetime.now()).strftime('%a %d %b %H:%M')}"
                  for e in matches[:5]]
        same = len({e.title() for e in matches}) == 1
        if not same:
            return ToolResult(False, error=(
                f"{title!r} matches several events: {'; '.join(titles)}. "
                f"Say which one."))
    ev = matches[0]
    s, e2 = _pydate(ev.startDate()), _pydate(ev.endDate())
    snapshot = {"title": ev.title(), "start": s.isoformat() if s else None,
                "end": e2.isoformat() if e2 else None,
                "location": ev.location() or "", "notes": (ev.notes() or "")[:500],
                "calendar": ev.calendar().title() if ev.calendar() else ""}
    journal.record("delete", f"calendar:{snapshot['title']}", snapshot=False,
                   session=REGISTRY.session_id, meta=snapshot)
    span = 1 if all_occurrences else 0        # 0 = this event, 1 = future events
    ok, err = _store().removeEvent_span_error_(ev, span, None)
    if not ok:
        return ToolResult(False, error=f"could not delete: {err}")
    return ToolResult(True, content={"deleted": snapshot},
                      display=f"deleted '{snapshot['title']}'"
                              + (f" on {s.strftime('%a %d %b %H:%M')}" if s else ""))


@REGISTRY.tool(
    "update_event",
    "Change an existing calendar event: move its time, rename it, or change its "
    "location.",
    {"type": "object", "properties": {
        "title": {"type": "string", "description": "Current title, or part of it"},
        "when": {"type": "string", "description": "Day to search, if known"},
        "new_title": {"type": "string"},
        "new_start": {"type": "string", "description": "ISO datetime"},
        "new_end": {"type": "string", "description": "ISO datetime"},
        "new_location": {"type": "string"}},
     "required": ["title"]},
    risk=Risk.MEDIUM, category="calendar_write", parallel_safe=False,
    assess=lambda a: Verdict(Risk.MEDIUM, "edits an existing calendar event",
                             category="calendar_write", needs_confirm=False))
def update_event(title: str, when: str = "", new_title: str = "",
                 new_start: str = "", new_end: str = "", new_location: str = ""):
    try:
        matches = _find_events(title, when)
    except PermissionError as e:
        return ToolResult(False, error=str(e))
    if not matches:
        return ToolResult(False, error=f"No event matching {title!r}")
    ev = matches[0]
    old_start = _pydate(ev.startDate())
    if new_title:
        ev.setTitle_(new_title)
    if new_location:
        ev.setLocation_(new_location)
    if new_start:
        try:
            s = dt.datetime.fromisoformat(new_start)
        except ValueError:
            return ToolResult(False, error=f"could not parse start {new_start!r}")
        dur = ((_pydate(ev.endDate()) - old_start) if old_start else
               dt.timedelta(hours=1))
        ev.setStartDate_(_nsdate(s))
        ev.setEndDate_(_nsdate(dt.datetime.fromisoformat(new_end) if new_end
                               else s + dur))
    elif new_end:
        ev.setEndDate_(_nsdate(dt.datetime.fromisoformat(new_end)))
    ok, err = _store().saveEvent_span_error_(ev, 0, None)
    if not ok:
        return ToolResult(False, error=f"could not save: {err}")
    ns = _pydate(ev.startDate())
    return ToolResult(True, content={
        "title": ev.title(),
        "start": ns.isoformat(timespec="minutes") if ns else None},
        display=f"updated '{ev.title()}'"
                + (f" -> {ns.strftime('%a %d %b %H:%M')}" if ns else ""))


@REGISTRY.tool(
    "delete_reminder",
    "Delete a reminder outright, rather than marking it done.",
    {"type": "object", "properties": {"title": {"type": "string"}},
     "required": ["title"]},
    risk=Risk.MEDIUM, category="calendar_write", parallel_safe=False,
    assess=lambda a: Verdict(Risk.MEDIUM, "deletes a reminder",
                             category="calendar_write", needs_confirm=False))
def delete_reminder(title: str):
    if not _grant("reminders"):
        return ToolResult(False, error="Reminders access is not granted.")
    st = _store()
    pred = st.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
        None, None, st.calendarsForEntityType_(1))
    done = threading.Event()
    box = {}

    def _cb(items):
        box["i"] = items
        done.set()

    st.fetchRemindersMatchingPredicate_completion_(pred, _cb)
    done.wait(25)
    q = title.strip().lower()
    for r in box.get("i") or []:
        if q in (r.title() or "").lower():
            name = r.title()
            ok, err = st.removeReminder_commit_error_(r, True, None)
            if not ok:
                return ToolResult(False, error=str(err))
            return ToolResult(True, content={"deleted": name},
                              display=f"deleted reminder '{name}'")
    return ToolResult(False, error=f"No open reminder matching {title!r}")
