import os, sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); import _isolate  # noqa: E401,E702
"""
Regression checks for the tool bugs found in the real usage log
(~/.jarvis/logs/daemon.log, Oct 2026). Offline: no network, no model, no
apps touched - services are replaced with fakes.
"""
import json
import tempfile
import time
from pathlib import Path

from jarvis.tools import REGISTRY  # noqa: F401  (registers every tool)

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:300]}"))
    R.append(bool(c))


T = Path(tempfile.mkdtemp(prefix="jarvis-audit-"))

# ------------------------------------------------- run_shell / python_eval
# A failed result reaches the model as its error string only; "exit 1" hid
# every traceback (21 run_shell + 5 python_eval failures in the log).
from jarvis.tools import shell

r = shell.run_shell("echo partial; echo 'boom: bad thing' >&2; exit 3", cwd=str(T))
check("run_shell failure says why (stderr in the error)",
      not r.ok and "exit 3" in r.error and "boom: bad thing" in r.error, r.error)
r = shell.run_shell("echo only-stdout; exit 2", cwd=str(T))
check("run_shell failure falls back to stdout", not r.ok and "only-stdout" in r.error, r.error)
r = shell.run_shell("echo fine", cwd=str(T))
check("run_shell success unchanged", r.ok and r.error == "" and "fine" in r.content["output"], r)
r = shell.python_eval("raise ValueError('kaboom')")
check("python_eval failure carries the traceback",
      not r.ok and "ValueError" in r.error and "kaboom" in r.error, r.error)
check("a signal kill is named as a stop, not a bare exit -15",
      "signal 15" in shell._fail_reason(-15, "", ""), shell._fail_reason(-15, "", ""))
long_err = "x" * 5000 + "LAST LINE"
check("long stderr keeps its end", shell._fail_reason(1, "", long_err).endswith("LAST LINE")
      and len(shell._fail_reason(1, "", long_err)) < 1700)

# ------------------------------------------------- browser_set_editor check
# 15 of 22 real pastes into LeetCode's Monaco editor "failed" although
# paste_text confirmed them: the check looked for the code's first line in the
# page text, which Monaco hides and renders with U+00A0 spaces.
from jarvis.tools import browser

java = """class Solution {
    public int reverse(int x) {
        int result = 0;
        while (x != 0) {
            int digit = x % 10;
            x /= 10;
            if (result > Integer.MAX_VALUE / 10 || result < Integer.MIN_VALUE / 10) return 0;
            result = result * 10 + digit;
        }
        return result;
    }
}"""
window = "\n".join(java.splitlines()[-6:])            # Monaco's text-area window at the end
check("Monaco window at the end of the code counts as landed",
      browser._pasted_code_seen(java, window, ""))
check("re-indented window still counts", browser._pasted_code_seen(
    java, "\n".join(l.strip() for l in java.splitlines()[-6:]), ""))
template = "class Solution {\n    public int reverse(int x) {\n        \n    }\n}"
check("the site's untouched template does not count",
      not browser._pasted_code_seen(java, template, ""))
check("leftover old text after the code does not count",
      not browser._pasted_code_seen(java, window + "\n// old junk line here", ""))
page_nbsp = "Description\n" + "        return result;".replace(" ", " ") + "\nSubmit"
check("rendered lines with non-breaking spaces count",
      browser._pasted_code_seen(java, "", page_nbsp))
py = "class Solution:\n    def twoSum(self, nums, target):\n        seen = {}\n        return [seen_index, i]"
check("the template's first line alone in the page is not proof",
      not browser._pasted_code_seen(py, "", "class Solution:\n    def twoSum(self):"))

# browser_js once scripting is known to be blocked: an actionable error
saved_ab = browser._active_browser
browser._active_browser = lambda: "Safari"
browser._JS_BLOCKED_AT["Safari"] = time.time()
try:
    r = browser.browser_js("document.title")
    check("blocked browser_js says what to use instead",
          not r.ok and "Do not retry" in r.error and "browser_read" in r.error
          and "Allow JavaScript from Apple Events" in r.error, r.error)
finally:
    browser._JS_BLOCKED_AT.pop("Safari", None)
    browser._active_browser = saved_ab

# ------------------------------------------------- search_papers
# 'ti:"X" AND au:"Y"' became 'all:ti:...' -> HTTP 400 (4 of 7 calls); arXiv
# timeouts and 429s had no fallback (2 more).
import requests

from jarvis.tools import web

check("arXiv field syntax passes through",
      web._arxiv_query('ti:"Deep Learning" and au:"LeCun"') == 'ti:"Deep Learning" AND au:"LeCun"',
      web._arxiv_query('ti:"Deep Learning" and au:"LeCun"'))
check("plain words still search all fields",
      web._arxiv_query("graph neural networks") == "all:graph neural networks")


class _Resp:
    def __init__(self, data=None, text="", status=200):
        self._d, self.text, self.status_code = data, text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")

    def json(self):
        return self._d


_openalex_hit = {"results": [{
    "title": "On the Dangers of Stochastic Parrots",
    "authorships": [{"author": {"display_name": "Emily M. Bender"}},
                    {"author": {"display_name": "Timnit Gebru"}}],
    "publication_date": "2021-03-01",
    "abstract_inverted_index": {"Large": [0], "language": [1], "models": [2]},
    "doi": "https://doi.org/10.1145/3442188.3445922", "cited_by_count": 5000}]}
calls = []


def fake_get(url, *a, **kw):
    calls.append(url)
    if "arxiv.org" in url:
        return _Resp(status=429)
    if "openalex.org" in url:
        return _Resp(_openalex_hit)
    raise AssertionError(url)


saved_get = requests.get
requests.get = fake_get
try:
    r = web.search_papers('ti:"On the Dangers of Stochastic Parrots"', 3)
finally:
    requests.get = saved_get
check("arXiv 429 falls back to OpenAlex",
      r.ok and r.content["source"] == "OpenAlex"
      and r.content["papers"][0]["authors"][0] == "Emily M. Bender"
      and r.content["papers"][0]["summary"] == "Large language models", r.error or r.content)
check("arXiv was asked over https with the field query intact",
      calls and calls[0].startswith("https://export.arxiv.org/") and "ti:%22On" in calls[0],
      calls[:1])

# ------------------------------------------------- find_images
# 9 of 21 calls "found nothing - try simpler words": long descriptive queries
# (Openverse needs every word) and Openverse timeouts (30 s) both read as that.
from jarvis.tools import documents_pro as dp

qs = dp._image_queries("India QR code digital payment merchant smartphone Wikimedia Commons")
check("long image queries get shorter forms without filler words",
      len(qs) >= 3 and "wikimedia" not in qs[1] and qs[2] == "india qr code", qs)

n_ov = []


def ov_timeout(q, n=10, commercial=True):
    n_ov.append(q)
    raise requests.Timeout("read timed out")


def commons_down(q, n=12):
    raise requests.ConnectionError("no route to host")


saved = dp._openverse, dp._commons
dp._openverse, dp._commons = ov_timeout, commons_down
try:
    r = dp.find_images("smartphone payment", 2)
finally:
    dp._openverse, dp._commons = saved
check("an image-service outage is reported as an outage, not bad wording",
      not r.ok and "did not answer" in r.error and "simpler words" not in r.error, r.error)
check("a timed-out service is not asked again for every variant", len(n_ov) == 1, n_ov)

_commons_json = {"query": {"pages": {
    "1": {"index": 1, "title": "File:QR code payment.jpg", "imageinfo": [{
        "thumburl": "https://upload.example/qr.jpg", "thumbwidth": 1800, "thumbheight": 1200,
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:QR_code_payment.jpg",
        "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"},
                        "Artist": {"value": "<a href='x'>Jane Doe</a>"},
                        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"}}}]},
    "2": {"index": 2, "title": "File:Old scan.png", "imageinfo": [{
        "url": "https://upload.example/old.png", "width": 900, "height": 700,
        "extmetadata": {"LicenseShortName": {"value": "GFDL"}}}]},
    "3": {"index": 3, "title": "File:Shop.jpg", "imageinfo": [{
        "url": "https://upload.example/shop.jpg", "width": 2000, "height": 1500,
        "extmetadata": {"LicenseShortName": {"value": "Public domain"}}}]}}}}
requests.get = lambda url, *a, **kw: _Resp(_commons_json)
try:
    rows = dp._commons("qr code payment")
finally:
    requests.get = saved_get
check("Commons rows carry license, version and creator; GFDL skipped",
      [(x["license"], x["license_version"]) for x in rows] == [("by-sa", "4.0"), ("pdm", "")]
      and rows[0]["creator"] == "Jane Doe" and rows[0]["title"] == "QR code payment", rows)

# ------------------------------------------------- move/copy/write paths
# move_file(main.java, src/com/example) renamed the file to an extensionless
# "example"; every later write into src/com/example/ then died with a raw
# FileExistsError.
from jarvis.tools import fs

src = T / "proj" / "src" / "java"
src.mkdir(parents=True)
(src / "main.java").write_text("package com.example;")
r = fs.move_file(str(src / "main.java"), str(T / "proj" / "src" / "com" / "example"))
dest = T / "proj" / "src" / "com" / "example" / "main.java"
check("move into a new package folder keeps the file name",
      r.ok and dest.is_file() and r.content["to"] == str(dest) and "note" in r.content,
      r.error or r.content)
(T / "README.md").write_text("hi")
r = fs.move_file(str(T / "README.md"), str(T / "README"))
check("dropping an extension in the same folder is still a rename",
      r.ok and (T / "README").is_file(), r.error or r.content)
(T / "notes").mkdir()
(T / "a.txt").write_text("a")
r = fs.copy_file(str(T / "a.txt"), str(T / "backup") + "/")
check("a trailing slash means a folder for copy_file",
      r.ok and (T / "backup" / "a.txt").is_file(), r.error or r.content)
(T / "blocker").write_text("I am a file")
r = fs.write_file(str(T / "blocker" / "Main.java"), "class Main {}")
check("writing under a file explains the path problem",
      not r.ok and "is a file, not a folder" in r.error, r.error or r.content)
r = fs.write_file(str(T / "notes"), "text")
check("writing onto a folder explains it", not r.ok and "is a folder" in r.error,
      r.error or r.content)
r = fs.write_file(str(T / "deep" / "new" / "ok.txt"), "fine")
check("normal writes still create parent folders",
      r.ok and (T / "deep" / "new" / "ok.txt").read_text() == "fine", r.error)

# ------------------------------------------------- quit_apps never targets JARVIS
# The daemon uses AppKit, so it is a foreground app called "Python": "quit
# everything" sent JARVIS itself a quit, then blamed a save dialog.
from jarvis.tools import control

fake_apps = [{"name": "Python", "pid": os.getpid(), "bundle_id": "org.python.python", "path": ""},
             {"name": "Notes", "pid": 999991, "bundle_id": "com.apple.Notes", "path": ""}]
saved_ra = control.running_apps
control.running_apps = lambda: list(fake_apps)
try:
    plan = control._plan_quit(None, [])
    plan2 = control._plan_quit(["Python"], None)
finally:
    control.running_apps = saved_ra
check("quit everything leaves JARVIS's own process alone",
      [t["name"] for t in plan["targets"]] == ["Notes"]
      and any("JARVIS itself" in k for k in plan["kept"]), plan)
check("quitting 'Python' by name never targets JARVIS", not plan2["targets"], plan2)


print("\n=== protected folders: running is fine, writing is not ===")
from jarvis.safety.policy import classify_shell as _cs, Risk as _R
check("running an app from /Applications is allowed",
      _cs("/Applications/Blender.app/Contents/MacOS/Blender -b /tmp/x.blend -o /tmp/r -f 1").risk < _R.FORBIDDEN)
check("moving something into /Applications is refused", _cs("mv ~/x.app /Applications/").risk == _R.FORBIDDEN)
check("copying an app out of /Applications is allowed", _cs("cp -R /Applications/Notes.app ~/Desktop").risk < _R.FORBIDDEN)
check("deleting inside /Applications is refused", _cs("rm -rf /Applications/Foo.app").risk == _R.FORBIDDEN)

print(f"\n{sum(R)}/{len(R)} tool audit checks passed")
sys.exit(0 if all(R) else 1)
