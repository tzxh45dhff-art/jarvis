"""
Contacts and messaging: iMessage, SMS, WhatsApp, Mail.

Contacts are read through the native Contacts framework and cached in memory.
The AppleScript equivalent takes about 17 seconds on this machine; the framework
takes milliseconds, and name resolution has to feel instant.

Sending is deliberately the most gated action in JARVIS. A message cannot be
unsent, so `send_message` always asks first and shows the resolved recipient
and the exact text that will go out.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse

from ..safety.policy import Risk, Verdict
from . import osa
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.comms")

_CN = {"store": None, "granted": None, "cache": None, "at": 0.0}
_LOCK = threading.Lock()
CACHE_TTL = 300


def _contacts_store():
    with _LOCK:
        if _CN["store"] is None:
            import Contacts as CN
            _CN["store"] = CN.CNContactStore.alloc().init()
        return _CN["store"]


def _grant_contacts() -> bool:
    # Only a grant is remembered. A refusal used to be cached for the life of
    # the engine, so approving Contacts afterwards changed nothing until a
    # restart.
    if _CN["granted"]:
        return True
    try:
        import Contacts as CN
        st = _contacts_store()
        status = CN.CNContactStore.authorizationStatusForEntityType_(0)
        if status == 3:                       # authorized
            _CN["granted"] = True
            return True
        done = threading.Event()
        box = {}

        def _cb(granted, err):
            # must return None: PyObjC types this handler as void
            box["g"] = bool(granted)
            done.set()

        if status in (1, 2):              # restricted / denied: asking again is silent
            return False
        st.requestAccessForEntityType_completionHandler_(0, _cb)
        done.wait(45)
        _CN["granted"] = bool(box.get("g")) or None
    except Exception as e:
        log.warning("contacts unavailable: %s", e)
        _CN["granted"] = None
    return bool(_CN["granted"])


def _all_contacts() -> list[dict]:
    """Load every contact once, then serve lookups from memory."""
    if _CN["cache"] is not None and time.time() - _CN["at"] < CACHE_TTL:
        return _CN["cache"]
    if not _grant_contacts():
        raise PermissionError(
            "Contacts access is not granted. Turn on 'JARVIS Companion' under "
            "System Settings > Privacy & Security > Contacts (tell the user "
            "exactly that), then ask again - no restart needed.")
    import Contacts as CN
    keys = [CN.CNContactGivenNameKey, CN.CNContactFamilyNameKey,
            CN.CNContactNicknameKey, CN.CNContactOrganizationNameKey,
            CN.CNContactPhoneNumbersKey, CN.CNContactEmailAddressesKey]
    req = CN.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
    out: list[dict] = []

    def visit(c, stop):
        name = " ".join(x for x in (c.givenName(), c.familyName()) if x).strip()
        if not name:
            name = c.nickname() or c.organizationName() or ""
        phones = [p.value().stringValue() for p in (c.phoneNumbers() or [])]
        emails = [str(e.value()) for e in (c.emailAddresses() or [])]
        if name and (phones or emails):
            out.append({"name": name, "nickname": c.nickname() or "",
                        "org": c.organizationName() or "",
                        "phones": phones, "emails": emails})
        return True

    st = _contacts_store()
    st.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, visit)
    _CN["cache"], _CN["at"] = out, time.time()
    return out


def _normalise_phone(p: str) -> str:
    return re.sub(r"[^\d+]", "", p or "")


_CALLING_CODES = {"IN": "91", "US": "1", "CA": "1", "GB": "44", "AU": "61",
                  "AE": "971", "SG": "65", "DE": "49", "FR": "33", "NZ": "64",
                  "PK": "92", "BD": "880", "NP": "977", "LK": "94", "SA": "966"}


_TZ_COUNTRY = {"Asia/Kolkata": "IN", "Asia/Calcutta": "IN", "Asia/Dubai": "AE",
               "Asia/Singapore": "SG", "Asia/Karachi": "PK", "Asia/Dhaka": "BD",
               "Asia/Kathmandu": "NP", "Asia/Colombo": "LK", "Asia/Riyadh": "SA",
               "Europe/London": "GB", "Europe/Berlin": "DE", "Europe/Paris": "FR",
               "Australia/Sydney": "AU", "Australia/Melbourne": "AU",
               "Pacific/Auckland": "NZ"}


def _home_code() -> str:
    """The user's own country calling code. The time zone decides first:
    English-language Macs commonly report a US locale wherever they are."""
    cc = ""
    try:
        tz = os.path.realpath("/etc/localtime").split("zoneinfo/")[-1]
        cc = _TZ_COUNTRY.get(tz, "")
        if not cc and tz.startswith("America/"):
            cc = "US"
    except Exception:
        pass
    if not cc:
        try:
            from Foundation import NSLocale
            cc = str(NSLocale.currentLocale().countryCode() or "")
        except Exception:
            cc = ""
    return _CALLING_CODES.get(cc.upper(), "91")


def _international(p: str) -> str:
    """Digits with the country code, as WhatsApp links need: contacts are
    usually saved as '98765 43210' or '098765 43210', which open an empty or
    wrong chat without one."""
    raw = _normalise_phone(p)
    if not raw:
        return ""
    if raw.startswith("+"):
        return raw[1:]
    if raw.startswith("00"):
        return raw[2:]
    digits = raw.lstrip("0")
    code = _home_code()
    if len(digits) <= 10:
        return code + digits
    return digits


# What people say vs how the contact is saved. Indian usage included, since
# "Papa" / "Mummy" / "Bhaiya" are as common as "Dad" / "Mom".
_FAMILY = [
    {"dad", "daddy", "papa", "pappa", "father", "pa", "pops", "pitaji", "abba", "baba", "dady"},
    {"mom", "mum", "mommy", "mummy", "mama", "mother", "ma", "maa", "mammi", "mumma", "amma", "ammi", "mataji"},
    {"brother", "bro", "bhai", "bhaiya", "bhaiyya", "veere", "veer"},
    {"sister", "sis", "didi", "behen", "dii", "di"},
    {"grandma", "granny", "nani", "dadi", "grandmother", "biji"},
    {"grandpa", "grandfather", "nana", "dadu", "dada", "bauji"},
    {"wife", "wifey", "hubby", "husband"},
]


def _query_variants(q: str) -> list[str]:
    q = re.sub(r"^(my|the)\s+", "", q.strip().lower())
    out = [q]
    for group in _FAMILY:
        if q in group:
            out += [w for w in sorted(group) if w != q]
    return out


def resolve_contact(query: str, limit: int = 5) -> list[dict]:
    """Fuzzy-match a name, nickname or organisation to contacts. Family words
    try their usual spellings ("dad" finds a contact saved as "Papa")."""
    variants = _query_variants(query or "")
    if not variants[0]:
        return []
    for q in variants:
        hits = _resolve_one(q, limit, exact_only=(q != variants[0]))
        if hits:
            return hits
    # "dad" may be saved under his name, with "father" recorded on the user's
    # own card (Contacts > My Card > related names).
    name = _related_name(variants[0])
    if name:
        hits = _resolve_one(name.lower(), limit)
        if hits:
            return [dict(h, via=f"your {variants[0]} on your contact card") for h in hits]
    return []


_RELATION_LABELS = [
    ({"dad", "daddy", "papa", "father", "pa", "pops", "pitaji", "abba", "baba"}, ("father", "parent")),
    ({"mom", "mum", "mommy", "mummy", "mama", "mother", "ma", "maa", "amma", "ammi"}, ("mother", "parent")),
    ({"brother", "bro", "bhai", "bhaiya"}, ("brother", "sibling")),
    ({"sister", "sis", "didi"}, ("sister", "sibling")),
    ({"wife", "husband", "wifey", "hubby"}, ("spouse", "partner", "wife", "husband")),
]


def _related_name(word: str) -> str:
    labels = next((l for words, l in _RELATION_LABELS if word in words), None)
    if not labels:
        return ""
    try:
        import Contacts as CN
        me = _contacts_store().unifiedMeContactWithKeysToFetch_error_(
            [CN.CNContactRelationsKey], None)
        me = me[0] if isinstance(me, tuple) else me
        for rel in (me.contactRelations() or []) if me else []:
            label = (rel.label() or "").lower()
            if any(l in label for l in labels):
                return str(rel.value().name())
    except Exception as e:                                # noqa: BLE001
        log.info("me-card relations unavailable: %s", e)
    return ""


def _resolve_one(q: str, limit: int, exact_only: bool = False) -> list[dict]:
    people = _all_contacts()
    scored = []
    for c in people:
        hay = [c["name"].lower(), c["nickname"].lower(), c["org"].lower()]
        best = 0.0
        for h in hay:
            if not h:
                continue
            if h == q:
                best = 1.0
            elif h.startswith(q) or q in h.split():
                best = max(best, 0.92)
            elif q in h:
                best = max(best, 0.8)
            else:
                best = max(best, difflib.SequenceMatcher(None, q, h).ratio())
        # A family-word variant must match a whole word ("ma" must not hit
        # "Mark"); the user's own words may match loosely.
        if exact_only:
            words = set(re.findall(r"[a-z]+", " ".join(hay)))
            if q not in words:
                continue
            best = max(best, 0.95)
        if best >= 0.62:
            scored.append((best, c))
    scored.sort(key=lambda x: -x[0])
    return [dict(c, score=round(s, 2)) for s, c in scored[:limit]]


@REGISTRY.tool(
    "find_contact",
    "Look up someone in Contacts by name and return their phone numbers and "
    "email addresses. Use this before sending anything, so the right recipient "
    "is chosen.",
    {"type": "object", "properties": {
        "name": {"type": "string"}, "limit": {"type": "integer"}},
     "required": ["name"]},
    risk=Risk.SAFE, category="read")
def find_contact(name: str, limit: int = 5):
    try:
        hits = resolve_contact(name, limit)
    except PermissionError as e:
        return ToolResult(False, error=str(e))
    except Exception as e:
        return ToolResult(False, error=f"contact lookup failed: {e}")
    if not hits:
        return ToolResult(True, content={"matches": [], "query": name},
                          display=f"no contact matching {name!r}")
    return ToolResult(True, content={"query": name, "matches": hits},
                      display=f"{len(hits)} match(es): "
                              + ", ".join(h["name"] for h in hits[:3]))


def _send_verdict(args: dict) -> Verdict:
    to = args.get("to", "")
    body = (args.get("text") or "")[:120]
    svc = args.get("service", "imessage")
    return Verdict(Risk.HIGH,
                   f"sends a {svc} message to {to} - this cannot be unsent",
                   category="send", needs_confirm=True,
                   detail=f'"{body}"')


def _imessage(handle: str, text: str, service: str) -> tuple[bool, str]:
    script = '''on run argv
  set target to item 1 of argv
  set msg to item 2 of argv
  set svcName to item 3 of argv
  tell application "Messages"
    if svcName is "sms" then
      set svc to first account whose service type = SMS
    else
      set svc to first account whose service type = iMessage
    end if
    set theBuddy to participant target of svc
    send msg to theBuddy
    return "sent"
  end tell
end run'''
    r = osa.run(script, handle, text, "sms" if service == "sms" else "imessage",
                timeout=40)
    return r.ok and "sent" in r.out, (r.error or r.out)


def _whatsapp(handle: str, text: str) -> tuple[bool, str]:
    """
    WhatsApp exposes no scripting dictionary. Its URL scheme opens the chat with
    the text pre-filled; sending it then needs one Return keystroke, which is
    why this path requires Accessibility as well as confirmation.
    """
    phone = _international(handle)
    if not phone:
        return False, "WhatsApp needs a phone number, not a name or email"
    url = ("whatsapp://send?phone=" + phone
           + "&text=" + urllib.parse.quote(text))
    subprocess.run(["open", url], capture_output=True, timeout=20)
    time.sleep(2.6)                       # let the chat open and focus the field
    r = osa.run('''on run argv
  tell application "System Events"
    tell process "WhatsApp"
      set frontmost to true
      delay 0.4
      key code 36
    end tell
  end tell
  return "sent"
end run''', timeout=25)
    if not r.ok:
        return False, (r.error + "  (the message is typed into WhatsApp but was "
                                 "not sent - press Return to send it)")
    return True, "sent"


@REGISTRY.tool(
    "send_message",
    "Send a message via iMessage, SMS or WhatsApp. The recipient may be a "
    "contact name, a phone number or an email address. Always confirmed with "
    "the user first, because a sent message cannot be recalled.",
    {"type": "object", "properties": {
        "to": {"type": "string",
               "description": "Contact name, phone number or email"},
        "text": {"type": "string", "description": "Exact message body"},
        "service": {"type": "string", "enum": ["imessage", "sms", "whatsapp"],
                    "description": "Default imessage"}},
     "required": ["to", "text"]},
    risk=Risk.HIGH, category="send", assess=_send_verdict, parallel_safe=False)
def send_message(to: str, text: str, service: str = "imessage"):
    service = (service or "imessage").lower()
    handle = to.strip()
    resolved_name = ""
    # A bare number or email is used as-is; anything else is a name to resolve.
    if not re.match(r"^[+\d][\d\s\-().]{5,}$", handle) and "@" not in handle:
        try:
            hits = resolve_contact(handle, 4)
        except PermissionError as e:
            return ToolResult(False, error=str(e))
        if not hits:
            return ToolResult(False, error=(
                f"No contact matching {to!r}. Give a phone number, or check the "
                f"name with find_contact first."))
        if len(hits) > 1 and hits[0]["score"] - hits[1]["score"] < 0.08:
            names = ", ".join(f"{h['name']}" for h in hits[:4])
            return ToolResult(False, error=(
                f"{to!r} is ambiguous - it could be: {names}. Ask the user which "
                f"one, then send using their exact name or number."))
        top = hits[0]
        resolved_name = top["name"]
        if service == "whatsapp":
            if not top["phones"]:
                return ToolResult(False, error=f"{top['name']} has no phone number")
            handle = top["phones"][0]
        elif service == "sms":
            if not top["phones"]:
                return ToolResult(False, error=f"{top['name']} has no phone number")
            handle = top["phones"][0]
        else:
            handle = (top["phones"] or top["emails"])[0]

    if service == "whatsapp":
        ok, msg = _whatsapp(handle, text)
    else:
        ok, msg = _imessage(handle, text, service)
    if not ok:
        return ToolResult(False, error=f"could not send: {msg}")
    return ToolResult(True, content={
        "sent": True, "to": resolved_name or handle, "handle": handle,
        "service": service, "text": text},
        display=f"sent {service} to {resolved_name or handle}")


@REGISTRY.tool(
    "read_messages",
    "Read recent iMessage/SMS conversations, or the recent messages with one "
    "person. Requires Full Disk Access.",
    {"type": "object", "properties": {
        "contact": {"type": "string", "description": "Optional: filter to one person"},
        "limit": {"type": "integer"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def read_messages(contact: str = "", limit: int = 20):
    import os
    import sqlite3
    db = os.path.expanduser("~/Library/Messages/chat.db")
    if not os.path.exists(db):
        return ToolResult(False, error="Messages database not found")
    handles = []
    if contact:
        try:
            hits = resolve_contact(contact, 3)
        except PermissionError as e:
            return ToolResult(False, error=str(e))
        for h in hits:
            handles += [_normalise_phone(p) for p in h["phones"]] + h["emails"]
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=8)
        q = """
        SELECT m.is_from_me, h.id,
               datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime'),
               COALESCE(m.text, '')
        FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE COALESCE(m.text,'') != ''
        ORDER BY m.date DESC LIMIT ?
        """
        rows = con.execute(q, (max(limit, 1) * (8 if handles else 1),)).fetchall()
        con.close()
    except sqlite3.OperationalError as e:
        return ToolResult(False, error=(
            f"Cannot read the Messages database ({e}). This needs Full Disk "
            f"Access for the app running JARVIS: System Settings > Privacy & "
            f"Security > Full Disk Access."))
    except Exception as e:
        return ToolResult(False, error=f"message read failed: {e}")

    out = []
    for is_me, hid, when, text in rows:
        hid = hid or ""
        if handles and not any(
                _normalise_phone(hid).endswith(h[-9:]) or hid == h
                for h in handles if h):
            continue
        out.append({"from": "me" if is_me else (hid or "unknown"),
                    "when": when, "text": text[:400]})
        if len(out) >= limit:
            break
    return ToolResult(True, content={"messages": list(reversed(out)),
                                     "count": len(out)},
                      display=f"{len(out)} message(s)"
                              + (f" with {contact}" if contact else ""))


@REGISTRY.tool(
    "send_email",
    "Compose and send an email through Mail. Always confirmed first.",
    {"type": "object", "properties": {
        "to": {"type": "string", "description": "Contact name or email address"},
        "subject": {"type": "string"},
        "body": {"type": "string"}},
     "required": ["to", "subject", "body"]},
    risk=Risk.HIGH, category="send", parallel_safe=False,
    assess=lambda a: Verdict(Risk.HIGH,
                             f"sends an email to {a.get('to')} - cannot be unsent",
                             category="send", needs_confirm=True,
                             detail=f"subject: {a.get('subject', '')[:80]}"))
def send_email(to: str, subject: str, body: str):
    addr = to.strip()
    name = ""
    if "@" not in addr:
        try:
            hits = resolve_contact(addr, 3)
        except PermissionError as e:
            return ToolResult(False, error=str(e))
        emails = [e for h in hits for e in h["emails"]]
        if not emails:
            return ToolResult(False, error=f"No email address found for {to!r}")
        name, addr = hits[0]["name"], emails[0]
    script = '''on run argv
  set addr to item 1 of argv
  set subj to item 2 of argv
  set bod to item 3 of argv
  tell application "Mail"
    set m to make new outgoing message with properties {subject:subj, content:bod, visible:false}
    tell m to make new to recipient at end of to recipients with properties {address:addr}
    send m
    return "sent"
  end tell
end run'''
    r = osa.run(script, addr, subject, body, timeout=60)
    if not r.ok:
        return ToolResult(False, error=r.error)
    return ToolResult(True, content={"to": name or addr, "address": addr,
                                     "subject": subject},
                      display=f"emailed {name or addr}: {subject[:50]}")


# NOTE: reading Mail is deliberately not exposed as a tool.
#
# Mail's AppleScript interface is slow in a way that cannot be worked around:
# on this machine (a 20,000-message inbox) fetching metadata for five messages
# takes ~18 s, and filtering for unread across a 40-message window times out
# past 60 s. Bodies are worse - `content of m` makes Mail fetch each one from
# the server. A tool that slow would get picked by the model and then stall the
# whole turn, so sending email is supported and reading is not.
#
# Reinstating it would mean going around Mail: reading the local Envelope Index
# SQLite store directly (needs Full Disk Access), or talking IMAP.
