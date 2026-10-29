"""
Creative tools against real media made on the spot: video, audio, images,
charts, presentations, Whisper and headless Blender.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.tools import REGISTRY

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:400]}"))
    R.append(bool(c))


def run(name, **kw):
    return REGISTRY.execute(name, kw)


T = Path(tempfile.mkdtemp(prefix="jarvis-media-"))
FF = "/opt/homebrew/bin/ffmpeg"


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


# fixtures: two 4 s coloured clips with a tone, a spoken sentence, a picture
for name, color, hz in (("a", "red", 440), ("b", "blue", 660)):
    sh(FF, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=640x360:d=4:r=30",
       "-f", "lavfi", "-i", f"sine=frequency={hz}:duration=4", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(T / f"{name}.mp4"))
sh("/usr/bin/say", "-o", str(T / "speech.aiff"), "Hello. This is a test of the transcription system. "
   "The quarterly revenue grew by twelve percent.")
from PIL import Image
Image.new("RGB", (800, 600), (30, 120, 220)).save(T / "photo.png")

print("=== video_tool ===")
r = run("video_tool", action="info", path=str(T / "a.mp4"))
check("info: duration and size", r.ok and abs(r.content["duration_seconds"] - 4) < 0.3
      and r.content["size"] == "640x360" and r.content["has_audio"], r.content if r.ok else r.error)
r = run("video_tool", action="trim", path=str(T / "a.mp4"), start="1", end="3")
check("trim: 2 s", r.ok and abs(r.content["duration_seconds"] - 2) < 0.3, r.content if r.ok else r.error)
r = run("video_tool", action="concat", paths=[str(T / "a.mp4"), str(T / "b.mp4")])
check("concat: 8 s with sound", r.ok and abs(r.content["duration_seconds"] - 8) < 0.5, r.content if r.ok else r.error)
joined = r.content["path"] if r.ok else str(T / "a.mp4")
r = run("video_tool", action="speed", path=str(T / "a.mp4"), factor=2)
check("speed x2: half as long", r.ok and abs(r.content["duration_seconds"] - 2) < 0.4, r.content if r.ok else r.error)
r = run("video_tool", action="crop_to", path=str(T / "a.mp4"), aspect="9:16")
check("crop to 9:16", r.ok, r.content if r.ok else r.error)
if r.ok:
    info = run("video_tool", action="info", path=r.content["path"]).content
    w, h = [int(x) for x in info["size"].split("x")]
    check("...and really vertical", abs(w / h - 9 / 16) < 0.02, info["size"])
r = run("video_tool", action="resize", path=str(T / "a.mp4"), width=320)
check("resize to 320 wide", r.ok and run("video_tool", action="info", path=r.content["path"]).content["size"].startswith("320x"), r.content if r.ok else r.error)
r = run("video_tool", action="mute", path=str(T / "a.mp4"))
check("mute removes audio", r.ok and not run("video_tool", action="info", path=r.content["path"]).content["has_audio"], r.content if r.ok else r.error)
r = run("video_tool", action="fade", path=str(T / "a.mp4"), fade_in=1, fade_out=1)
check("fade in/out", r.ok, r.content if r.ok else r.error)
r = run("video_tool", action="thumbnails", path=str(T / "a.mp4"), every=1)
check("thumbnails: a frame per second", r.ok and r.content["count"] >= 3, r.content if r.ok else r.error)
r = run("video_tool", action="gif", path=str(T / "a.mp4"), start="0", end="2")
check("gif", r.ok and r.content["path"].endswith(".gif"), r.content if r.ok else r.error)
r = run("video_tool", action="extract_audio", path=str(T / "a.mp4"))
check("extract audio", r.ok and r.content["path"].endswith(".m4a"), r.content if r.ok else r.error)
r = run("video_tool", action="replace_audio", path=str(T / "a.mp4"), audio=str(T / "speech.aiff"))
check("replace audio", r.ok, r.content if r.ok else r.error)
check("trim past the end is refused", not run("video_tool", action="trim", path=str(T / "a.mp4"), start="9").ok)
check("a non-media file is refused", not run("video_tool", action="info", path=str(T / "photo.png.txt")).ok)
check("the original is untouched", (T / "a.mp4").exists())

print("\n=== transcribe_media (Whisper, offline) ===")
r = run("transcribe_media", path=str(T / "speech.aiff"))
try:
    import mlx_whisper  # noqa: F401  (optional: requirements-local.txt)
    HAVE_WHISPER = True
except ImportError:
    HAVE_WHISPER = False
    check("without mlx-whisper the tool says what to install",
          not r.ok and "requirements-local.txt" in r.error, r.error if not r.ok else r.content)
if HAVE_WHISPER:
    txt = (r.content.get("text", "") if r.ok else "").lower()
    check("speech becomes text",
          r.ok and "transcription" in txt and "twelve percent" in txt.replace("12%", "twelve percent"),
          r.content if r.ok else r.error)
    check("a .srt is written with timestamps",
          r.ok and "-->" in Path(r.content["srt"]).read_text())
    r2 = run("transcribe_media", path=str(T / "a.mp4"))
    check("a clip with only a tone says there is no speech (or returns nothing)",
          (not r2.ok) or len(r2.content.get("text", "")) < 40, r2.content if r2.ok else r2.error)

print("\n=== image_edit / image_combine ===")
r = run("image_edit", path=str(T / "photo.png"), ops=[
    {"op": "crop", "aspect": "1:1"}, {"op": "resize", "width": 400}, {"op": "grayscale"},
    {"op": "text", "text": "Hello", "position": "bottom"}, {"op": "border", "width": 10, "color": "#FF0000"}])
check("a chain of edits is applied", r.ok and r.content["applied"] == ["crop", "resize", "grayscale", "text", "border"],
      r.content if r.ok else r.error)
if r.ok:
    im = Image.open(r.content["path"])
    check("size is right: 400 square plus a 10 px border", im.size == (420, 420), im.size)
check("an unknown op alone is refused", not run("image_edit", path=str(T / "photo.png"), ops=[{"op": "teleport"}]).ok)
r = run("image_combine", paths=[str(T / "photo.png")] * 4, layout="grid", cell=200)
check("combine into a grid", r.ok and r.content["size"] == "448x348", r.content if r.ok else r.error)

print("\n=== make_chart ===")
for kind in ("column", "bar", "line", "pie", "area"):
    r = run("make_chart", kind=kind, title=f"{kind} chart", output=str(T / f"{kind}.png"),
            data={"categories": ["Q1", "Q2", "Q3"], "series": [{"name": "2026", "values": [4, 7, 5]}]})
    check(f"{kind} chart is drawn", r.ok and Image.open(r.content["path"]).size[0] > 800, r.content if r.ok else r.error)
r = run("make_chart", kind="line", data={"categories": ["a"], "series": [{"name": "x", "values": ["oops"]}]})
check("non-numeric values are refused", not r.ok)
(T / "d.csv").write_text("month,sales,cost\nJan,10,6\nFeb,14,8\nMar,9,7\n")
r = run("make_chart", kind="column", file=str(T / "d.csv"), output=str(T / "csvchart.png"))
check("a chart straight from a CSV", r.ok, r.content if r.ok else r.error)

print("\n=== presentations ===")
r = run("make_presentation", path=str(T / "deck.pptx"), theme="dark", slides=[
    {"layout": "title", "title": "Quarterly review", "subtitle": "Q3 2026"},
    {"layout": "bullets", "title": "Highlights", "bullets": ["Revenue up 12%", "Churn down", {"text": "Launches", "sub": ["Voice HUD", "Web answers"]}], "notes": "Say the revenue number slowly."},
    {"layout": "chart", "title": "Revenue", "chart": {"type": "column", "categories": ["Q1", "Q2", "Q3"], "series": [{"name": "USD m", "values": [4, 7, 9]}]}},
    {"layout": "image", "title": "Chart image", "image": str(T / "column.png"), "caption": "From make_chart"},
    {"layout": "closing", "title": "Thank you"}])
check("a 5-slide deck is created and verified", r.ok and r.content["slides"] == 5 and r.content["verified"], r.content if r.ok else r.error)
deck = r.content["path"] if r.ok else ""
rd = run("read_document", path=deck)
check("the deck reads back with titles, bullets and notes",
      rd.ok and "Quarterly review" in rd.content["content"] and "Voice HUD" in rd.content["content"]
      and "revenue number" in rd.content["content"], rd.content if rd.ok else rd.error)
r = run("edit_presentation", path=deck, ops=[
    {"op": "add_slide", "after": 1, "slide": {"layout": "quote", "quote": "Ship it.", "author": "Everyone"}},
    {"op": "replace_text", "find": "Churn down", "replace": "Churn down 3 points"},
    {"op": "move_slide", "from": 6, "to": 2},
    {"op": "set_notes", "index": 1, "notes": "Welcome everyone."}])
check("edits are applied to a copy", r.ok and r.content["slides"] == 6 and r.content["path"] != deck
      and "-edited" in r.content["path"], r.content if r.ok else r.error)
rd = run("read_document", path=r.content["path"]) if r.ok else None
check("...and show in the file", rd is not None and rd.ok and "Churn down 3 points" in rd.content["content"]
      and "Ship it" in rd.content["content"], rd.content if rd and rd.ok else "")
r = run("edit_presentation", path=deck, ops=[{"op": "delete_slide", "index": 99}])
check("an impossible edit is refused, not silently ignored", not r.ok)
check("the original deck is untouched", len(__import__("pptx").Presentation(deck).slides) == 5)
r = run("make_presentation", path=str(T / "bad.pptx"), slides=[])
check("an empty deck is refused", not r.ok)
r = run("make_presentation", path=str(T / "warn.pptx"), slides=[
    {"layout": "image", "title": "Missing", "image": "/nope/missing.png"}])
check("a missing image is a warning, not a crash", r.ok and r.content.get("warnings"), r.content if r.ok else r.error)
pv = run("presentation_preview", path=deck)
check("a preview image can be rendered", pv.ok and Path(pv.content["image"]).exists(), pv.content if pv.ok else pv.error)

print("\n=== blender (headless) ===")
if os.path.exists("/Applications/Blender.app"):
    v = REGISTRY.assess("blender_script", {"code": "print(1)"})
    check("running a Blender script asks first", v.needs_confirm)
    REGISTRY.confirm_hook = lambda name, args, verdict: True     # the user says yes
    r = run("blender_script", code="import bpy\nprint('JARVIS_BLENDER', bpy.app.version_string)\n"
            "bpy.ops.mesh.primitive_cube_add()\nprint('objects', len(bpy.data.objects))")
    check("a script runs and reports", r.ok and "JARVIS_BLENDER" in r.content["output"] and "objects 4" in r.content["output"],
          r.content if r.ok else r.error)
    r = run("blender_script", code="raise RuntimeError('boom')")
    check("a failing script is reported, not hidden", not r.ok and "boom" in r.error, r.error if not r.ok else r.content)
    # characters come from figure_kit: a sculpted, clothed figure in a lit studio
    fig_png = T / "figure.png"
    r = run("blender_script", code=(
        "import bpy\nfrom figure_kit import figure, studio, render_settings\n"
        "studio()\n"
        "figure({'gender': 'female', 'hair': {'style': 'long'}, 'top': {'type': 'kurta', 'color': 'red'},"
        " 'bottom': {'type': 'trousers'}, 'pose': 'hands_on_hips', 'beard': False, 'accessories': ['glasses', 'watch']})\n"
        "render_settings('preview', 216, 270)\nbpy.context.scene.cycles.samples = 4\n"
        "names = {o.name for o in bpy.data.objects}\n"
        "print('FIG', all(n in names for n in ('Body', 'Head', 'Hair', 'HairCurtain', 'Top', 'KurtaSkirt',"
        " 'Bottom', 'Palm_l', 'Eye_1', 'DisplayBase', 'Key', 'Camera')))\n"
        "b = bpy.data.objects['Body']\nprint('VERTS', len(b.data.vertices))\n"),
        render_output=str(fig_png), timeout=300)
    out = r.content.get("output", "") if r.ok else ""
    check("figure_kit builds a full character (body, face, hair, clothes, studio)",
          r.ok and "FIG True" in out, r.content if r.ok else r.error)
    check("the character renders", r.ok and r.content.get("render") and Path(r.content["render"]).stat().st_size > 5000,
          r.content if r.ok else r.error)
    for pose in ("wave", "walking", "arms_crossed", "hero"):
        r = run("blender_script", code=(
            "from figure_kit import figure\n"
            f"figure({{'pose': '{pose}', 'hair': {{'style': 'curly'}}, 'top': {{'type': 'jacket', 'inner': 'white'}},"
            " 'bottom': {'type': 'shorts'}, 'shoes': {'type': 'boots'}, 'accessories': ['cap', 'backpack']})\nprint('OK')"))
        check(f"pose '{pose}' with mixed outfit builds", r.ok and "OK" in r.content["output"], r.content if r.ok else r.error)
else:
    print("  (Blender not installed - skipped)")

print(f"\n{sum(R)}/{len(R)} creative-tool checks passed")
sys.exit(0 if all(R) else 1)
