"""
Creative media tools: video, audio, images, charts, 3D.

Each tool takes a *description of the edit* and does it with the right engine -
ffmpeg for video and sound, Pillow for pictures, matplotlib for charts, Whisper
for speech, Blender (headless) for 3D - so a request like "cut the first 20
seconds and make it vertical" is one verified call instead of a hand-built
command line.

Guardrails: outputs are always new files next to the source (nothing is
overwritten), every result is checked to exist and be non-empty, and anything
that runs arbitrary code (Blender scripts) asks first.

Honest limits: this Mac's ffmpeg has no `subtitles`/`drawtext` filters, so
captions can be added as a selectable subtitle track but not burned into the
picture.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..safety import journal
from ..safety.policy import Risk, Verdict, classify_path_write
from .files_plus import _assess_out, _finish, _parse_pages, _run, _unique, _which
from .fs import _p
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.media_edit")


def _ffprobe(path: Path) -> dict:
    probe = _which("ffprobe")
    if not probe:
        return {}
    r = _run([probe, "-v", "quiet", "-print_format", "json", "-show_format",
              "-show_streams", str(path)], 30)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}


def _has_stream(info: dict, kind: str) -> bool:
    return any(s.get("codec_type") == kind for s in info.get("streams", []))


def _duration(info: dict) -> float:
    try:
        return float(info.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def _secs(t) -> float:
    s = 0.0
    for part in str(t).strip().split(":"):
        s = s * 60 + float(part)
    return s


def _atempo_chain(factor: float) -> str:
    """atempo only accepts 0.5-2.0 per stage; chain stages for anything else."""
    parts, f = [], factor
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


# ================================================================== video

VIDEO_ACTIONS = ("info", "trim", "concat", "speed", "resize", "crop_to", "rotate",
                 "mute", "replace_audio", "add_audio", "fade", "thumbnails",
                 "compress", "add_subtitles", "overlay_image", "extract_audio", "gif")


@REGISTRY.tool(
    "video_tool",
    "Edit video and audio files with ffmpeg; always writes a NEW file next to "
    "the source. action: info | trim (start, end) | concat (paths) | speed "
    "(factor, e.g. 2 = double speed) | resize (width or height) | crop_to "
    "(aspect '9:16', '1:1', '16:9' - centred crop for social formats) | rotate "
    "(angle 90/180/270) | mute | replace_audio (audio) | add_audio (audio, "
    "mixed under the original) | fade (fade_in, fade_out seconds) | thumbnails "
    "(every N seconds or at 'times') | compress (crf 18-32, higher = smaller) | "
    "add_subtitles (srt, as a selectable track) | overlay_image (image, "
    "position tl/tr/bl/br/center) | extract_audio | gif. Times are seconds or "
    "'mm:ss'.",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": list(VIDEO_ACTIONS)},
        "path": {"type": "string"},
        "paths": {"type": "array", "items": {"type": "string"}},
        "start": {"type": "string"}, "end": {"type": "string"},
        "factor": {"type": "number"},
        "width": {"type": "integer"}, "height": {"type": "integer"},
        "aspect": {"type": "string"}, "angle": {"type": "integer"},
        "audio": {"type": "string"}, "srt": {"type": "string"}, "image": {"type": "string"},
        "position": {"type": "string"},
        "fade_in": {"type": "number"}, "fade_out": {"type": "number"},
        "every": {"type": "number"}, "times": {"type": "array", "items": {"type": "string"}},
        "crf": {"type": "integer"},
        "output": {"type": "string"}},
     "required": ["action"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False, slow=True,
    assess=_assess_out)
def video_tool(action: str, path: str = "", paths: list | None = None, start: str = "",
               end: str = "", factor: float = 0, width: int = 0, height: int = 0,
               aspect: str = "", angle: int = 90, audio: str = "", srt: str = "",
               image: str = "", position: str = "br", fade_in: float = 0,
               fade_out: float = 0, every: float = 0, times: list | None = None,
               crf: int = 26, output: str = ""):
    ff = _which("ffmpeg")
    if not ff:
        return ToolResult(False, error="ffmpeg is not installed (brew install ffmpeg)")
    action = (action or "").lower()
    if action not in VIDEO_ACTIONS:
        return ToolResult(False, error=f"unknown action {action!r}")
    t0 = time.time()

    def out_for(src: Path, suffix: str, ext: str | None = None) -> Path:
        e = ext or src.suffix or ".mp4"
        return _unique(_p(output) if output else src.with_name(f"{src.stem}-{suffix}{e}"))

    def encode(cmd: list[str], out: Path, timeout: int = 1800):
        r = _run(cmd + [str(out)], timeout)
        if r.returncode != 0:
            out.unlink(missing_ok=True)
            raise RuntimeError((r.stderr or "ffmpeg failed").strip()[-300:])

    def done(out: Path, note: str = "") -> ToolResult:
        r = _finish(out)
        info = _ffprobe(out)
        r["duration_seconds"] = round(_duration(info), 1)
        r["seconds_to_make"] = round(time.time() - t0, 1)
        if note:
            r["note"] = note
        return ToolResult(True, content=r,
                          display=f"{action}: {out.name} ({r['duration_seconds']}s, "
                                  f"{r['bytes'] / 1e6:.1f} MB)")

    try:
        if action == "concat":
            srcs = [_p(x) for x in (paths or [])]
            if len(srcs) < 2:
                return ToolResult(False, error="give at least two files in 'paths'")
            for s in srcs:
                if not s.exists():
                    return ToolResult(False, error=f"File not found: {s}")
            infos = [_ffprobe(s) for s in srcs]
            with_audio = all(_has_stream(i, "audio") for i in infos)
            n = len(srcs)
            # normalise every clip to the first one's size and rate so differently
            # shaped clips join cleanly
            vs = next((s for s in infos[0].get("streams", []) if s.get("codec_type") == "video"), {})
            w, h = int(vs.get("width") or 1280), int(vs.get("height") or 720)
            w, h = w - w % 2, h - h % 2
            parts, labels = [], ""
            cmd = [ff, "-y", "-v", "error"]
            for s in srcs:
                cmd += ["-i", str(s)]
            for i in range(n):
                parts.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]")
                labels += f"[v{i}]" + (f"[{i}:a]" if with_audio else "")
            filt = ";".join(parts) + f";{labels}concat=n={n}:v=1:a={1 if with_audio else 0}[v]" \
                   + ("[a]" if with_audio else "")
            cmd += ["-filter_complex", filt, "-map", "[v]"]
            if with_audio:
                cmd += ["-map", "[a]"]
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]
            if with_audio:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
            out = out_for(srcs[0], "joined", ".mp4")
            encode(cmd, out)
            return done(out, "" if with_audio else "one or more clips had no audio, so the result is silent")

        src = _p(path)
        if not src.exists():
            return ToolResult(False, error=f"File not found: {src}")
        info = _ffprobe(src)
        if not info:
            return ToolResult(False, error=f"{src.name} is not a media file ffmpeg can read")
        dur = _duration(info)
        has_v, has_a = _has_stream(info, "video"), _has_stream(info, "audio")

        if action == "info":
            vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
            return ToolResult(True, content={
                "path": str(src), "duration_seconds": round(dur, 2),
                "size": f"{vs.get('width')}x{vs.get('height')}" if vs else None,
                "video_codec": vs.get("codec_name"), "has_audio": has_a,
                "bytes": src.stat().st_size,
                "fps": vs.get("r_frame_rate")}, display=f"{src.name}: {dur:.1f}s")

        if action == "trim":
            s_t = _secs(start or 0)
            e_t = _secs(end) if end else dur
            if e_t <= s_t or s_t >= dur:
                return ToolResult(False, error=f"bad range: the file is {dur:.1f}s long")
            out = out_for(src, "trim")
            encode([ff, "-y", "-v", "error", "-ss", str(s_t), "-i", str(src),
                    "-t", str(e_t - s_t), "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-c:a", "aac"] if has_v else
                   [ff, "-y", "-v", "error", "-ss", str(s_t), "-i", str(src),
                    "-t", str(e_t - s_t)], out)
            return done(out)

        if action == "speed":
            f = float(factor or 0)
            if not 0.1 <= f <= 16:
                return ToolResult(False, error="factor must be between 0.1 and 16")
            out = out_for(src, f"x{f:g}")
            cmd = [ff, "-y", "-v", "error", "-i", str(src)]
            if has_v and has_a:
                cmd += ["-filter_complex", f"[0:v]setpts=PTS/{f}[v];[0:a]{_atempo_chain(f)}[a]",
                        "-map", "[v]", "-map", "[a]"]
            elif has_v:
                cmd += ["-vf", f"setpts=PTS/{f}"]
            else:
                cmd += ["-af", _atempo_chain(f)]
            encode(cmd, out)
            return done(out)

        if action in ("resize", "crop_to", "rotate", "fade") and not has_v:
            return ToolResult(False, error=f"{src.name} has no video")

        if action == "resize":
            if not (width or height):
                return ToolResult(False, error="give width and/or height")
            vf = f"scale={width or -2}:{height or -2}"
            if width and not height:
                vf = f"scale={width}:-2"
            out = out_for(src, f"{width or 'auto'}x{height or 'auto'}")
            encode([ff, "-y", "-v", "error", "-i", str(src), "-vf", vf, "-c:v", "libx264",
                    "-preset", "fast", "-crf", "21", "-c:a", "copy"], out)
            return done(out)

        if action == "crop_to":
            m = re.fullmatch(r"\s*(\d+)\s*[:/x]\s*(\d+)\s*", aspect or "")
            if not m:
                return ToolResult(False, error="aspect like '9:16', '1:1' or '16:9'")
            a, b = int(m.group(1)), int(m.group(2))
            vf = (f"crop='if(gt(iw/ih,{a}/{b}),ih*{a}/{b},iw)':'if(gt(iw/ih,{a}/{b}),ih,iw*{b}/{a})',"
                  f"scale=trunc(iw/2)*2:trunc(ih/2)*2")
            out = out_for(src, f"{a}x{b}")
            encode([ff, "-y", "-v", "error", "-i", str(src), "-vf", vf, "-c:v", "libx264",
                    "-preset", "fast", "-crf", "21", "-c:a", "copy"], out)
            return done(out)

        if action == "rotate":
            vf = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}.get(angle % 360)
            if not vf:
                return ToolResult(False, error="angle must be 90, 180 or 270")
            out = out_for(src, f"rot{angle % 360}")
            encode([ff, "-y", "-v", "error", "-i", str(src), "-vf", vf, "-c:v", "libx264",
                    "-preset", "fast", "-crf", "21", "-c:a", "copy"], out)
            return done(out)

        if action == "mute":
            out = out_for(src, "mute")
            encode([ff, "-y", "-v", "error", "-i", str(src), "-an", "-c:v", "copy"], out)
            return done(out)

        if action in ("replace_audio", "add_audio"):
            au = _p(audio)
            if not au.exists():
                return ToolResult(False, error=f"audio file not found: {au}")
            out = out_for(src, "audio")
            if action == "replace_audio" or not has_a:
                cmd = [ff, "-y", "-v", "error", "-i", str(src), "-i", str(au),
                       "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest"]
            else:
                cmd = [ff, "-y", "-v", "error", "-i", str(src), "-i", str(au),
                       "-filter_complex", "[1:a]volume=0.35[m];[0:a][m]amix=inputs=2:duration=first[a]",
                       "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac"]
            encode(cmd, out)
            return done(out)

        if action == "fade":
            fi, fo = float(fade_in or 0), float(fade_out or 0)
            if not (fi or fo):
                return ToolResult(False, error="give fade_in and/or fade_out seconds")
            vf = []
            af = []
            if fi:
                vf.append(f"fade=t=in:st=0:d={fi}")
                af.append(f"afade=t=in:st=0:d={fi}")
            if fo:
                vf.append(f"fade=t=out:st={max(0, dur - fo):.2f}:d={fo}")
                af.append(f"afade=t=out:st={max(0, dur - fo):.2f}:d={fo}")
            out = out_for(src, "fade")
            cmd = [ff, "-y", "-v", "error", "-i", str(src), "-vf", ",".join(vf)]
            if has_a:
                cmd += ["-af", ",".join(af)]
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "21"]
            encode(cmd, out)
            return done(out)

        if action == "thumbnails":
            folder = _unique(_p(output) if output else src.with_name(src.stem + "-frames"))
            folder.mkdir(parents=True, exist_ok=True)
            stamps = [str(t) for t in (times or [])]
            if not stamps:
                step = float(every or max(1.0, dur / 8))
                stamps = [f"{t:.2f}" for t in [i * step for i in range(int(dur // step) + 1)]
                          if t < dur][:60]
            made = []
            for i, t in enumerate(stamps[:60]):
                png = folder / f"{src.stem}-{i + 1:03d}.png"
                r = _run([ff, "-y", "-v", "error", "-ss", str(_secs(t)), "-i", str(src),
                          "-frames:v", "1", str(png)], 60)
                if r.returncode == 0 and png.exists():
                    made.append(png.name)
            if not made:
                return ToolResult(False, error="no frames were extracted")
            journal.record("create", str(folder), snapshot=False, session=REGISTRY.session_id)
            return ToolResult(True, content={"folder": str(folder), "files": made, "count": len(made),
                                             "verified": True}, display=f"extracted {len(made)} frames")

        if action == "compress":
            c = max(18, min(34, int(crf or 26)))
            out = out_for(src, "small")
            encode([ff, "-y", "-v", "error", "-i", str(src), "-c:v", "libx264", "-preset", "slow",
                    "-crf", str(c), "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"], out)
            r = done(out)
            r.content["original_bytes"] = src.stat().st_size
            r.content["saved_percent"] = round(100 * (1 - r.content["bytes"] / max(1, src.stat().st_size)), 1)
            return r

        if action == "add_subtitles":
            sp = _p(srt)
            if not sp.exists():
                return ToolResult(False, error=f"subtitle file not found: {sp}")
            out = out_for(src, "subs", ".mp4" if src.suffix.lower() in (".mp4", ".m4v", ".mov") else ".mkv")
            codec = "mov_text" if out.suffix == ".mp4" else "srt"
            encode([ff, "-y", "-v", "error", "-i", str(src), "-i", str(sp), "-map", "0",
                    "-map", "1:0", "-c", "copy", "-c:s", codec], out)
            return done(out, "added as a selectable subtitle track (this ffmpeg cannot burn "
                             "captions into the picture)")

        if action == "overlay_image":
            ip = _p(image)
            if not ip.exists():
                return ToolResult(False, error=f"image not found: {ip}")
            pos = {"tl": "20:20", "tr": "W-w-20:20", "bl": "20:H-h-20", "br": "W-w-20:H-h-20",
                   "center": "(W-w)/2:(H-h)/2"}.get((position or "br").lower())
            if not pos:
                return ToolResult(False, error="position: tl, tr, bl, br or center")
            out = out_for(src, "overlay")
            encode([ff, "-y", "-v", "error", "-i", str(src), "-i", str(ip), "-filter_complex",
                    f"[1:v]scale=iw*min(1\\,(0.18*main_w)/iw):-1[o];[0:v][o]overlay={pos}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-c:a", "copy"], out) \
                if False else encode([ff, "-y", "-v", "error", "-i", str(src), "-i", str(ip),
                                      "-filter_complex", f"[0:v][1:v]overlay={pos}",
                                      "-c:v", "libx264", "-preset", "fast", "-crf", "21",
                                      "-c:a", "copy"], out)
            return done(out)

        if action == "extract_audio":
            if not has_a:
                return ToolResult(False, error=f"{src.name} has no audio")
            out = out_for(src, "audio", ".m4a")
            encode([ff, "-y", "-v", "error", "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "192k"], out)
            return done(out)

        if action == "gif":
            out = out_for(src, "clip", ".gif")
            s_t = _secs(start or 0)
            cmd = [ff, "-y", "-v", "error", "-ss", str(s_t), "-i", str(src)]
            if end:
                cmd += ["-t", str(max(0.1, _secs(end) - s_t))]
            cmd += ["-vf", "fps=12,scale=480:-1:flags=lanczos", "-loop", "0"]
            encode(cmd, out)
            return done(out)
    except Exception as e:                        # noqa: BLE001
        return ToolResult(False, error=f"{action} failed: {e}"[:400])
    return ToolResult(False, error=f"unhandled action {action!r}")


# ================================================================== transcription

def _fmt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@REGISTRY.tool(
    "transcribe_media",
    "Transcribe speech from an audio or video file to text, entirely on this "
    "Mac (Whisper). Writes a .txt and a .srt subtitle file next to the media "
    "and returns the text. Handles meetings, lectures, voice notes, interviews. "
    "Set language (e.g. 'hi', 'es') for non-English; default is English.",
    {"type": "object", "properties": {
        "path": {"type": "string"},
        "language": {"type": "string", "description": "ISO code, default en"},
        "accurate": {"type": "boolean", "description": "Use the larger, slower model"}},
     "required": ["path"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False, slow=True,
    assess=_assess_out)
def transcribe_media(path: str, language: str = "en", accurate: bool = False):
    src = _p(path)
    if not src.exists():
        return ToolResult(False, error=f"File not found: {src}")
    info = _ffprobe(src)
    if info and not _has_stream(info, "audio"):
        return ToolResult(False, error=f"{src.name} has no audio track")
    dur = _duration(info)
    lang = (language or "en").lower()[:2]
    if accurate or lang != "en":
        model = "mlx-community/whisper-large-v3-turbo"
    else:
        model = "mlx-community/whisper-small.en-mlx"
    t0 = time.time()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        import mlx_whisper
    except ImportError:
        return ToolResult(False, error=(
            "transcription needs the on-device speech model: "
            "pip install -r requirements-local.txt (installs mlx-whisper; Apple Silicon only)"))
    try:
        res = mlx_whisper.transcribe(str(src), path_or_hf_repo=model,
                                     language=None if model.endswith("small.en-mlx") else lang,
                                     verbose=None)
    except Exception as e:                        # noqa: BLE001
        return ToolResult(False, error=f"transcription failed: {e}"[:300])
    segs = res.get("segments") or []
    text = (res.get("text") or "").strip()
    if not text:
        return ToolResult(False, error="no speech was found in the file")
    txt_path = _unique(src.with_suffix(".txt"))
    srt_path = _unique(src.with_suffix(".srt"))
    txt_path.write_text(text + "\n")
    lines = []
    for i, s in enumerate(segs, 1):
        lines.append(f"{i}\n{_fmt_ts(s['start'])} --> {_fmt_ts(s['end'])}\n{s['text'].strip()}\n")
    srt_path.write_text("\n".join(lines))
    for f in (txt_path, srt_path):
        journal.record("create", str(f), snapshot=False, session=REGISTRY.session_id)
    return ToolResult(True, content={
        "text": text[:6000], "truncated": len(text) > 6000, "characters": len(text),
        "txt": str(txt_path), "srt": str(srt_path), "segments": len(segs),
        "language": res.get("language", lang), "audio_seconds": round(dur, 1),
        "seconds_to_transcribe": round(time.time() - t0, 1), "model": model.split("/")[-1],
        "verified": txt_path.exists() and srt_path.exists()},
        display=f"transcribed {src.name} ({len(text):,} chars in {time.time() - t0:.0f}s)")


# ================================================================== images

def _color(c, default="#000000"):
    return c or default


IMAGE_OPS = ("resize", "crop", "rotate", "flip", "grayscale", "brightness", "contrast",
             "saturation", "sharpness", "blur", "sharpen", "auto_contrast", "invert",
             "sepia", "text", "border", "round_corners", "pad", "fit", "thumbnail")


@REGISTRY.tool(
    "image_edit",
    "Edit an image with a list of operations applied in order; saves a NEW file. "
    "ops entries are objects with 'op' and its parameters: resize {width, "
    "height} (keeps ratio if one is given) | fit {width, height} (cover-crop to "
    "exact size) | crop {x, y, width, height} or {aspect:'1:1'} (centred) | "
    "rotate {degrees} | flip {direction:'h'|'v'} | grayscale | sepia | invert | "
    "brightness/contrast/saturation/sharpness {factor, 1=unchanged} | blur "
    "{radius} | sharpen | auto_contrast | text {text, x, y, size, color, "
    "position:'top'|'bottom'|'center'} | border {width, color} | round_corners "
    "{radius} | pad {width, height, color} | thumbnail {size}.",
    {"type": "object", "properties": {
        "path": {"type": "string"},
        "ops": {"type": "array", "items": {"type": "object"}},
        "format": {"type": "string", "description": "png, jpg or webp; default same as source"},
        "output": {"type": "string"}},
     "required": ["path", "ops"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False, slow=True, assess=_assess_out)
def image_edit(path: str, ops: list, format: str = "", output: str = ""):
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
    src = _p(path)
    if not src.exists():
        return ToolResult(False, error=f"File not found: {src}")
    try:
        im = Image.open(src)
        im = ImageOps.exif_transpose(im)
    except Exception as e:
        return ToolResult(False, error=f"cannot open image: {e}"[:200])
    applied, warnings = [], []

    def font(size: int):
        for cand in ("/System/Library/Fonts/Helvetica.ttc",
                     "/System/Library/Fonts/Supplemental/Arial.ttf",
                     "/Library/Fonts/Arial.ttf"):
            if os.path.exists(cand):
                try:
                    return ImageFont.truetype(cand, size)
                except Exception:
                    continue
        return ImageFont.load_default()

    for op in ops or []:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).lower()
        try:
            if kind == "resize":
                w, h = int(op.get("width") or 0), int(op.get("height") or 0)
                if w and not h:
                    h = round(im.height * w / im.width)
                elif h and not w:
                    w = round(im.width * h / im.height)
                if not (w and h):
                    raise ValueError("give width and/or height")
                im = im.resize((w, h), Image.LANCZOS)
            elif kind == "fit":
                w, h = int(op["width"]), int(op["height"])
                im = ImageOps.fit(im.convert("RGBA") if im.mode == "P" else im, (w, h), Image.LANCZOS)
            elif kind == "crop":
                if op.get("aspect"):
                    a, b = [float(x) for x in re.split(r"[:/x]", str(op["aspect"]))]
                    target = a / b
                    cur = im.width / im.height
                    if cur > target:
                        nw = round(im.height * target)
                        x0 = (im.width - nw) // 2
                        im = im.crop((x0, 0, x0 + nw, im.height))
                    else:
                        nh = round(im.width / target)
                        y0 = (im.height - nh) // 2
                        im = im.crop((0, y0, im.width, y0 + nh))
                else:
                    x, y = int(op.get("x", 0)), int(op.get("y", 0))
                    im = im.crop((x, y, x + int(op["width"]), y + int(op["height"])))
            elif kind == "rotate":
                im = im.rotate(-float(op.get("degrees", 90)), expand=True)
            elif kind == "flip":
                im = ImageOps.mirror(im) if str(op.get("direction", "h")).lower().startswith("h") \
                    else ImageOps.flip(im)
            elif kind == "grayscale":
                im = ImageOps.grayscale(im).convert("RGB")
            elif kind == "sepia":
                g = ImageOps.grayscale(im)
                im = ImageOps.colorize(g, "#2b1a0a", "#f4e3c1")
            elif kind == "invert":
                im = ImageOps.invert(im.convert("RGB"))
            elif kind in ("brightness", "contrast", "saturation", "sharpness"):
                cls = {"brightness": ImageEnhance.Brightness, "contrast": ImageEnhance.Contrast,
                       "saturation": ImageEnhance.Color, "sharpness": ImageEnhance.Sharpness}[kind]
                im = cls(im.convert("RGB")).enhance(float(op.get("factor", 1.2)))
            elif kind == "blur":
                im = im.filter(ImageFilter.GaussianBlur(float(op.get("radius", 4))))
            elif kind == "sharpen":
                im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=2))
            elif kind == "auto_contrast":
                im = ImageOps.autocontrast(im.convert("RGB"))
            elif kind == "text":
                im = im.convert("RGBA")
                d = ImageDraw.Draw(im)
                size = int(op.get("size") or max(18, im.width // 18))
                f = font(size)
                txt = str(op.get("text", ""))
                bbox = d.textbbox((0, 0), txt, font=f)
                tw, th_ = bbox[2] - bbox[0], bbox[3] - bbox[1]
                pos = str(op.get("position", "")).lower()
                x = op.get("x")
                y = op.get("y")
                if x is None or y is None:
                    x = (im.width - tw) // 2
                    y = {"top": size, "center": (im.height - th_) // 2}.get(pos, im.height - th_ - size)
                col = _color(op.get("color"), "#FFFFFF")
                if op.get("shadow", True):
                    d.text((int(x) + 2, int(y) + 2), txt, font=f, fill="#000000AA")
                d.text((int(x), int(y)), txt, font=f, fill=col)
            elif kind == "border":
                im = ImageOps.expand(im.convert("RGB"), border=int(op.get("width", 12)),
                                     fill=_color(op.get("color"), "#FFFFFF"))
            elif kind == "round_corners":
                r = int(op.get("radius", 40))
                im = im.convert("RGBA")
                mask = Image.new("L", im.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, *im.size), r, fill=255)
                im.putalpha(mask)
            elif kind == "pad":
                w, h = int(op["width"]), int(op["height"])
                bg = Image.new("RGB", (w, h), _color(op.get("color"), "#FFFFFF"))
                c = im.copy().convert("RGBA")
                c.thumbnail((w, h))
                bg.paste(c, ((w - c.width) // 2, (h - c.height) // 2), c)
                im = bg
            elif kind == "thumbnail":
                im = im.copy()
                im.thumbnail((int(op.get("size", 256)),) * 2)
            else:
                warnings.append(f"unknown op {kind!r}")
                continue
            applied.append(kind)
        except Exception as e:                    # noqa: BLE001
            warnings.append(f"{kind}: {e}")
    if not applied:
        return ToolResult(False, error="nothing was applied: " + "; ".join(warnings or ["no ops"]))
    fmt = (format or src.suffix.lstrip(".") or "png").lower().replace("jpeg", "jpg")
    out = _unique(_p(output) if output else src.with_name(f"{src.stem}-edited.{fmt}"))
    try:
        if fmt in ("jpg",) and im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, "#FFFFFF")
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        im.save(out, quality=92)
        r = _finish(out)
    except Exception as e:                        # noqa: BLE001
        return ToolResult(False, error=f"could not save: {e}"[:200])
    r.update({"applied": applied, "size": f"{im.width}x{im.height}"})
    if warnings:
        r["warnings"] = warnings
    return ToolResult(True, content=r, display=f"edited image: {', '.join(applied)}")


@REGISTRY.tool(
    "image_combine",
    "Combine several images into one: layout grid | row | column, with a gap "
    "and background colour. Saves a new file.",
    {"type": "object", "properties": {
        "paths": {"type": "array", "items": {"type": "string"}},
        "layout": {"type": "string", "enum": ["grid", "row", "column"]},
        "columns": {"type": "integer"}, "gap": {"type": "integer"},
        "background": {"type": "string"}, "cell": {"type": "integer",
                                                   "description": "Longest side of each image, default 600"},
        "output": {"type": "string"}},
     "required": ["paths"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False, slow=True, assess=_assess_out)
def image_combine(paths: list, layout: str = "grid", columns: int = 0, gap: int = 16,
                  background: str = "#FFFFFF", cell: int = 600, output: str = ""):
    from PIL import Image
    files = [_p(x) for x in (paths or [])]
    if len(files) < 2:
        return ToolResult(False, error="give at least two images")
    ims = []
    for f in files:
        if not f.exists():
            return ToolResult(False, error=f"File not found: {f}")
        try:
            i = Image.open(f).convert("RGB")
            i.thumbnail((cell, cell))
            ims.append(i)
        except Exception as e:
            return ToolResult(False, error=f"cannot open {f.name}: {e}"[:160])
    n = len(ims)
    layout = (layout or "grid").lower()
    cols = n if layout == "row" else 1 if layout == "column" else (columns or math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)
    cw, ch = max(i.width for i in ims), max(i.height for i in ims)
    sheet = Image.new("RGB", (cols * cw + (cols + 1) * gap, rows * ch + (rows + 1) * gap), background)
    for k, i in enumerate(ims):
        r, c = divmod(k, cols)
        sheet.paste(i, (gap + c * (cw + gap) + (cw - i.width) // 2,
                        gap + r * (ch + gap) + (ch - i.height) // 2))
    out = _unique(_p(output) if output else files[0].with_name(files[0].stem + "-combined.png"))
    sheet.save(out)
    r = _finish(out)
    r["size"] = f"{sheet.width}x{sheet.height}"
    return ToolResult(True, content=r, display=f"combined {n} images")


# ================================================================== charts

@REGISTRY.tool(
    "make_chart",
    "Draw a clean chart and save it as a PNG (use it in documents and "
    "presentations). kind: bar | column | line | pie | scatter | area. Give "
    "data as {categories:[...], series:[{name, values:[...]}]} (for scatter: "
    "series values are y and categories are x numbers), or a csv/xlsx path "
    "with x_column and y_columns.",
    {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["bar", "column", "line", "pie", "scatter", "area"]},
        "data": {"type": "object"},
        "file": {"type": "string"}, "x_column": {"type": "string"},
        "y_columns": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"}, "x_label": {"type": "string"}, "y_label": {"type": "string"},
        "dark": {"type": "boolean"}, "output": {"type": "string"}},
     "required": ["kind"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False, slow=True, assess=_assess_out)
def make_chart(kind: str, data: dict | None = None, file: str = "", x_column: str = "",
               y_columns: list | None = None, title: str = "", x_label: str = "",
               y_label: str = "", dark: bool = False, output: str = ""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    kind = (kind or "column").lower()
    cats: list = []
    series: list[dict] = []
    try:
        if file:
            import pandas as pd
            f = _p(file)
            df = pd.read_csv(f) if f.suffix.lower() in (".csv", ".tsv") else pd.read_excel(f)
            xc = x_column or df.columns[0]
            ycs = y_columns or [c for c in df.columns if c != xc and
                                pd.api.types.is_numeric_dtype(df[c])][:6]
            cats = df[xc].tolist()
            series = [{"name": str(c), "values": df[c].tolist()} for c in ycs]
        else:
            cats = list((data or {}).get("categories", []))
            series = list((data or {}).get("series", []))
    except Exception as e:
        return ToolResult(False, error=f"cannot read the data: {e}"[:200])
    if not cats or not series:
        return ToolResult(False, error="need data: categories and at least one series")
    bg, fg, grid = ("#111114", "#F5F5F7", "#2A2A30") if dark else ("#FFFFFF", "#1D1D1F", "#E5E5EA")
    palette = ["#0A84FF", "#FF9F0A", "#30D158", "#BF5AF2", "#FF453A", "#64D2FF", "#FFD60A"]
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=170)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    labels = [str(c) for c in cats]
    x = list(range(len(cats)))
    try:
        if kind == "pie":
            vals = [float(v) for v in series[0]["values"]]
            ax.pie(vals, labels=labels, colors=palette[:len(vals)], autopct="%1.0f%%",
                   startangle=90, textprops={"color": fg},
                   wedgeprops={"linewidth": 2, "edgecolor": bg})
            ax.axis("equal")
        elif kind in ("bar", "column"):
            n = len(series)
            width = 0.8 / n
            for si, s in enumerate(series):
                vals = [float(v) for v in s["values"]]
                off = [i - 0.4 + width * (si + 0.5) for i in x]
                if kind == "column":
                    ax.bar(off, vals, width * 0.92, label=s.get("name"), color=palette[si % 7])
                else:
                    ax.barh([o for o in off], vals, width * 0.92, label=s.get("name"),
                            color=palette[si % 7])
            if kind == "column":
                ax.set_xticks(x, labels)
            else:
                ax.set_yticks(x, labels)
        elif kind in ("line", "area"):
            for si, s in enumerate(series):
                vals = [float(v) for v in s["values"]]
                ax.plot(x, vals, marker="o", linewidth=2.4, label=s.get("name"), color=palette[si % 7])
                if kind == "area":
                    ax.fill_between(x, vals, alpha=0.18, color=palette[si % 7])
            ax.set_xticks(x, labels)
        elif kind == "scatter":
            xs = [float(c) for c in cats]
            for si, s in enumerate(series):
                ax.scatter(xs, [float(v) for v in s["values"]], s=46, label=s.get("name"),
                           color=palette[si % 7])
        else:
            return ToolResult(False, error=f"unknown chart kind {kind!r}")
    except (ValueError, TypeError) as e:
        plt.close(fig)
        return ToolResult(False, error=f"the values must be numbers: {e}"[:200])
    if kind != "pie":
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(grid)
        ax.tick_params(colors=fg)
        ax.yaxis.grid(True, color=grid, linewidth=0.8)
        ax.set_axisbelow(True)
        if x_label:
            ax.set_xlabel(x_label, color=fg)
        if y_label:
            ax.set_ylabel(y_label, color=fg)
        if len(series) > 1:
            ax.legend(frameon=False, labelcolor=fg)
    if title:
        ax.set_title(title, color=fg, fontsize=16, fontweight="bold", pad=14)
    fig.tight_layout()
    default = Path(os.path.expanduser("~/Desktop")) / f"chart-{int(time.time())}.png"
    out = _unique(_p(output) if output else default)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=bg)
    plt.close(fig)
    r = _finish(out)
    return ToolResult(True, content=r, display=f"drew a {kind} chart: {out.name}")


# ================================================================== blender

BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"
# figure_kit (characters + studio) lives here; every script can import it.
BLENDER_KIT_DIR = str(Path(__file__).resolve().parent.parent / "blender")


def _assess_blender(args: dict) -> Verdict:
    return Verdict(Risk.MEDIUM, "runs a Python script inside Blender, which can read "
                   "and write files", category="exec", needs_confirm=True)


@REGISTRY.tool(
    "blender_script",
    "Run a Python (bpy) script in headless Blender 5 - modelling, materials, "
    "animation, import/export, and rendering without opening the app. Provide "
    "'code'; optionally a .blend file to open first (it is only saved if the "
    "script saves it) and render_output to render the current frame to a PNG "
    "after the script. Returns Blender's printed output.",
    {"type": "object", "properties": {
        "code": {"type": "string", "description": "Python using bpy"},
        "blend_file": {"type": "string"},
        "render_output": {"type": "string", "description": "PNG path to render to"},
        "timeout": {"type": "integer", "description": "Seconds, default 300"}},
     "required": ["code"]},
    risk=Risk.MEDIUM, category="exec", parallel_safe=False, slow=True,
    assess=_assess_blender)
def blender_script(code: str, blend_file: str = "", render_output: str = "", timeout: int = 300):
    if not os.path.exists(BLENDER):
        return ToolResult(False, error="Blender is not installed in /Applications")
    cmd = [BLENDER, "-b", "--factory-startup"]
    if blend_file:
        bf = _p(blend_file)
        if not bf.exists():
            return ToolResult(False, error=f"File not found: {bf}")
        cmd = [BLENDER, "-b", str(bf)]
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(f"import sys\nsys.path.insert(0, {BLENDER_KIT_DIR!r})\n" + code + "\n")
        script = fh.name
    cmd += ["--python", script]
    out_png = None
    if render_output:
        out_png = _unique(_p(render_output).with_suffix(".png"))
        cmd += ["-o", str(out_png.with_suffix("")) + "_####", "-F", "PNG", "-f", "1"] \
            if False else ["--render-output", str(out_png.with_suffix("")), "-F", "PNG", "-f", "1"]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=max(10, min(int(timeout or 300), 1800)), errors="replace")
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"Blender took longer than {timeout}s")
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
    text = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
    # Blender prints a lot of banner text; keep what the script and errors said.
    keep = [l for l in text.splitlines()
            if l.strip() and not l.startswith(("Blender ", "Read prefs", "Warning: ",
                                               "Color management", "ALSA", "Saved:"))
            and "| Fra:" not in l and not re.match(r"^\s*Fra:\d+", l)]   # render progress spam
    tail = "\n".join(keep[-60:])[-4000:]
    ok = r.returncode == 0 and "Traceback" not in text
    content = {"exit_code": r.returncode, "output": tail,
               "seconds": round(time.time() - t0, 1)}
    if out_png:
        # Blender names frame files with the frame number appended.
        cand = sorted(out_png.parent.glob(out_png.stem + "*.png"))
        if cand:
            content["render"] = str(cand[-1])
            journal.record("create", str(cand[-1]), snapshot=False, session=REGISTRY.session_id)
    if not ok:
        return ToolResult(False, content=content,
                          error="the script failed:\n" + "\n".join(
                              l for l in tail.splitlines()[-12:]))
    return ToolResult(True, content=content, display="ran a Blender script"
                      + (f" and rendered {Path(content['render']).name}" if content.get("render") else ""))
