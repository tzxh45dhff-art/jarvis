"""
The toolbox is larger than a provider will accept in one request. Deferred
tools must stay out of the request, be findable by plain words, and work once
found.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.tools import REGISTRY
from jarvis.tools import toolsearch as TS

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:300]}"))
    R.append(bool(c))


print("=== the request stays under the provider limit ===")
REGISTRY._enabled.clear()
offered = REGISTRY.schemas()
total = REGISTRY.schemas(include_deferred=True)
check(f"offered tools ({len(offered)}) leave room under 128 for found tools", len(offered) <= 114, len(offered))
check("even with every deferred tool switched on, the cap holds",
      len(offered) + REGISTRY.MAX_ENABLED <= 128, (len(offered), REGISTRY.MAX_ENABLED))
check("the deferred set is non-empty and all of it exists",
      all(REGISTRY.get(n) is not None for n in TS.DEFERRED), [n for n in TS.DEFERRED if not REGISTRY.get(n)])
check("find_tools itself is always offered",
      any(s["function"]["name"] == "find_tools" for s in offered))
check("the daily-driver tools are never deferred",
      not any(REGISTRY.get(n).deferred for n in ("web_answer", "web_search", "read_file", "run_shell",
                                                 "open_app", "make_presentation", "see_screen",
                                                 "show_steps", "read_document", "timer")))
check("the list is sorted, so the provider's prompt cache stays valid",
      [s["function"]["name"] for s in offered] == sorted(s["function"]["name"] for s in offered))
check("every tool name is unique", len({s["function"]["name"] for s in total}) == len(total))

print("\n=== the prompt lists what can be found ===")
from jarvis.core import prompts
sp = prompts.system_prompt()
check("the system prompt names the deferred tools", "- video_tool:" in sp and "- network_info:" in sp)
check("and says how to get them", "find_tools" in sp)
check("the system prompt is identical between calls (cache-safe)", sp == prompts.system_prompt())

print("\n=== finding them ===")
cases = {
    "trim a video clip": "video_tool",
    "transcribe this recording": "transcribe_media",
    "move this window to the left half": "window_manage",
    "run this every morning": "schedule_task",
    "resize and crop a photo": "image_edit",
    "how much disk space is left": "disk_usage",
    "run a blender python script": "blender_script",
}
for q, want in cases.items():
    REGISTRY._enabled.clear()
    hits = [n for _, n in TS.search(q)]
    check(f"{q!r} finds {want}", want in hits[:3], hits)

print("\n=== using them ===")
REGISTRY._enabled.clear()
before_names = REGISTRY.schemas()
before = len(before_names)
r = REGISTRY.execute("find_tools", {"query": "trim a video"})
check("find_tools succeeds and reports what it loaded", r.ok and any(t["name"] == "video_tool" for t in r.content["now_available"]), r.content if r.ok else r.error)
check("the loaded tool is now offered to the model",
      any(s["function"]["name"] == "video_tool" for s in REGISTRY.schemas()))
check("a common tool is not hidden behind a search",
      all(s in {x["function"]["name"] for x in before_names} for s in ("make_chart", "processes", "time_in", "youtube_tool")))
check("only the found tools were added", len(REGISTRY.schemas()) - before <= 6)
r = REGISTRY.execute("video_tool", {"action": "info", "path": "/nonexistent.mp4"})
check("and it runs (here: refusing a missing file politely)", (not r.ok) and "not found" in r.error.lower(), r.error)
r = REGISTRY.execute("find_tools", {"query": "zzzz qqqq"})
check("a query that matches nothing says what exists instead", not r.ok and "extra tools are" in r.error, r.error)
check("a deferred tool can still be executed by name even before it is found",
      REGISTRY.get("define_word") is not None)
for i, n in enumerate(sorted(TS.DEFERRED)):
    REGISTRY.enable([n])
check("enabling everything is capped", len(REGISTRY._enabled) <= REGISTRY.MAX_ENABLED, len(REGISTRY._enabled))
REGISTRY._enabled.clear()

print("\n=== tools a request plainly needs are loaded up front ===")
from jarvis.tools.toolsearch import preload_for
REGISTRY._enabled.clear()
check("a Blender request loads blender_script before the first step",
      "blender_script" in preload_for("make a 3d donut in Blender") and
      any(t["function"]["name"] == "blender_script" for t in REGISTRY.schemas()))
check("trimming a video loads video_tool", "video_tool" in preload_for("trim this video to the first 10 seconds"))
check("zip / transcribe / collage / leetcode load their tools",
      {"archive_tool", "transcribe_media", "image_combine", "leetcode_next"} <=
      set(preload_for("unzip it, transcribe the lecture, make a collage, next leetcode problem")))
REGISTRY._enabled.clear()
check("an ordinary request loads nothing extra", preload_for("what's the weather in Delhi") == []
      and preload_for("open my resume") == [])
REGISTRY._enabled.clear()

print(f"\n{sum(R)}/{len(R)} tool-search checks passed")
sys.exit(0 if all(R) else 1)
