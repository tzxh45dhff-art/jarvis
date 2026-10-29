"""`jarvis doctor` - check the machine is set up for everything JARVIS can do."""
from __future__ import annotations

import importlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .ui import console as ui

OK, WARN, BAD = "✓", "!", "✗"


def _row(state: str, name: str, detail: str = "") -> None:
    style = {"✓": "#4ade80", "!": "#fbbf24", "✗": "#f87171"}[state]
    ui.console.print(f"  [{style}]{state}[/] {name:<30} [dim]{detail}[/dim]")


def _check_screen_recording() -> tuple[str, str]:
    import tempfile
    p = Path(tempfile.mkdtemp()) / "t.png"
    try:
        subprocess.run(["screencapture", "-x", "-t", "png", str(p)],
                       capture_output=True, timeout=20)
        if p.exists() and p.stat().st_size > 0:
            return OK, "screen capture works"
    except Exception as e:
        return BAD, str(e)[:60]
    return BAD, ("not granted — System Settings › Privacy & Security › "
                 "Screen Recording › enable your terminal, then restart it")


def _check_accessibility() -> tuple[str, str]:
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application '
             'process whose frontmost is true'],
            capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return WARN, ("macOS is showing an approval dialog — accept it, or grant "
                      "Accessibility to your terminal in System Settings")
    except Exception as e:
        return BAD, str(e)[:60]
    if r.returncode == 0 and r.stdout.strip():
        return OK, f"front app: {r.stdout.strip()}"
    return BAD, ("not granted — System Settings › Privacy & Security › "
                 "Accessibility › enable your terminal")


def _check_mic() -> tuple[str, str]:
    try:
        import sounddevice as sd
        ins = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        if not ins:
            return BAD, "no input device"
        return OK, f"{len(ins)} input device(s), default: {ins[0]['name']}"
    except Exception as e:
        return BAD, str(e)[:60]


def run_doctor() -> int:
    from .config import CONFIG, DATA_DIR, JARVIS_HOME

    ui.banner("system check")
    ui.console.print("[bold]environment[/bold]")
    _row(OK, "macOS", platform.mac_ver()[0] + f" / {platform.machine()}")
    _row(OK, "python", sys.version.split()[0])
    _row(OK, "jarvis home", str(JARVIS_HOME))

    ui.console.print("\n[bold]python packages[/bold]")
    for mod, why in [("mlx", "Apple Silicon tensor runtime"),
                     ("mlx_lm", "text generation"),
                     ("mlx_vlm", "vision"),
                     ("mlx_audio", "Kokoro text-to-speech"),
                     ("mlx_whisper", "speech recognition"),
                     ("openwakeword", "wake word"),
                     ("onnxruntime", "wake word / VAD runtime"),
                     ("sentence_transformers", "memory embeddings"),
                     ("sounddevice", "audio i/o"),
                     ("trafilatura", "web page extraction"),
                     ("ddgs", "web search"),
                     ("reportlab", "PDF generation"),
                     ("Quartz", "mouse & keyboard control"),
                     ("rich", "terminal UI")]:
        try:
            importlib.import_module(mod)
            _row(OK, mod, why)
        except Exception as e:
            _row(BAD, mod, f"MISSING ({e}) — {why}")

    ui.console.print("\n[bold]models[/bold]")
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    for repo, why in [(CONFIG.llm.model, "reasoning + vision"),
                      (CONFIG.voice.tts_model, "speech output"),
                      (CONFIG.voice.stt_model, "speech input"),
                      (CONFIG.memory.embed_model, "memory search")]:
        d = hub / ("models--" + repo.replace("/", "--"))
        if d.exists():
            seen = set()
            size = 0
            for f in d.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    st = f.stat()
                    if st.st_ino not in seen:
                        seen.add(st.st_ino)
                        size += st.st_size
            _row(OK, repo.split("/")[-1], f"{size / 1e9:.1f} GB — {why}")
        else:
            _row(WARN, repo.split("/")[-1], f"not cached, will download — {why}")
    try:
        import openwakeword
        d = Path(openwakeword.__file__).parent / "resources" / "models"
        n = len(list(d.glob("*.onnx")))
        _row(OK if n else BAD, "wake word models", f"{n} onnx model(s)")
    except Exception as e:
        _row(BAD, "wake word models", str(e)[:50])

    ui.console.print("\n[bold]permissions[/bold]")
    for label, fn in [("screen recording", _check_screen_recording),
                      ("accessibility", _check_accessibility),
                      ("microphone", _check_mic)]:
        s, d = fn()
        _row(s, label, d)

    ui.console.print("\n[bold]optional tools[/bold]")
    for b, why in [("rg", "faster code search"), ("pandoc", "richer PDF export"),
                   ("git", "repository work"), ("node", "javascript projects")]:
        p = shutil.which(b)
        _row(OK if p else WARN, b, p or "not installed (optional)")

    ui.console.print()
    return 0
