"""
A small, separate process that speaks replies with feeling.

Kept out of the engine on purpose: the engine is already the heaviest thing on
the machine, and a voice model is only needed while something is being said.
This process loads Kokoro on the first request, speaks, and exits by itself
after a couple of idle minutes, handing the memory back.

Protocol: one JSON object per line over ~/.jarvis/run/tts.sock
    {"cmd": "say", "text": "...", "style": "bubbly"}
    {"cmd": "stop"} | {"cmd": "ping"} | {"cmd": "quit"}

    python -m jarvis.voice.speak_helper            run the server
    python -m jarvis.voice.speak_helper --demo     speak a sample of each mood
    python -m jarvis.voice.speak_helper --render D write sample WAVs to D
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

# Everything this needs is already cached; asking the network first cost ~2.5 s
# the first time each voice was used.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np

from . import mood
from .tts import KokoroTTS, Speaker

log = logging.getLogger("jarvis.speak")
SOCK = Path.home() / ".jarvis" / "run" / "tts.sock"
IDLE_EXIT_SECONDS = 150

SAMPLES = [
    ("cheerful", "Hi! I'm all set. What can I do for you today?"),
    ("excited", "All done! Your problem is solved and the tests passed."),
    ("curious", "Which one did you mean, the blue folder or the red one?"),
    ("apologetic", "I'm sorry, I couldn't open that. It looks like the file moved."),
    ("calm", "Careful, this can't be undone. Do you want me to go ahead?"),
]


def _fade(audio: np.ndarray, sr: int = 24000, ms: float = 6.0) -> np.ndarray:
    """Short fade at both ends so chunk joins never click."""
    n = min(int(sr * ms / 1000), len(audio) // 2)
    if n <= 0:
        return audio
    out = audio.copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out[:n] *= ramp
    out[-n:] *= ramp[::-1]
    return out


def _level(audio: np.ndarray, target: float = 0.8, max_gain: float = 2.5) -> np.ndarray:
    """Even out loudness between voices so a bright sentence is not quieter
    than a soft one just because of the voice that spoke it."""
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    if peak < 1e-4:
        return audio
    return (audio * min(target / peak, max_gain)).astype(np.float32)


class ExpressiveVoice:
    def __init__(self, tts: KokoroTTS | None = None, speaker: Speaker | None = None):
        self.tts = tts or KokoroTTS()
        self.speaker = speaker or Speaker(backend=self.tts)
        self._gen = 0
        self._lock = threading.Lock()
        self.ready = threading.Event()

    @property
    def speaking(self) -> bool:
        return self.speaker.speaking

    def warm(self) -> None:
        try:
            # Load the voices the default style leans on, so the first real
            # sentence never pays for them.
            for v, sp in {mood.STYLES[mood.DEFAULT_STYLE][m] for m in
                          ("neutral", "excited", "apologetic")}:
                self.tts.synthesize("Ready.", voice=v, speed=sp)
        except Exception as e:                       # pragma: no cover
            log.warning("warm-up failed: %s", e)
        self.ready.set()

    def synth_plan(self, text: str, style: str):
        """(audio, Delivery) per sentence, in order."""
        sr = self.tts.sample_rate
        for sent, d in mood.plan(text, style):
            audio = self.tts.synthesize(sent, voice=d.voice, speed=d.speed)
            if not len(audio):
                continue
            gap = np.zeros(int(d.pause * sr), dtype=np.float32)
            yield np.concatenate([_fade(_level(audio), sr), gap]), d

    def say(self, text: str, style: str = mood.DEFAULT_STYLE) -> None:
        """Speak, replacing anything still being said."""
        with self._lock:
            self._gen += 1
            gen = self._gen
        self.speaker.stop()
        self.speaker.begin()
        try:
            for audio, d in self.synth_plan(text, style):
                if gen != self._gen:
                    return                      # superseded or stopped
                log.info("%-10s %s@%.2f %d ms", d.mood, d.voice, d.speed,
                         int(len(audio) / self.tts.sample_rate * 1000))
                self.speaker.enqueue(audio)
        except Exception as e:
            log.warning("speak failed: %s", e)

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
        self.speaker.stop()


class Server:
    def __init__(self, voice: ExpressiveVoice):
        self.voice = voice
        self.last_activity = time.time()
        self._running = True
        self._srv: socket.socket | None = None

    def _reply(self, conn: socket.socket, obj: dict) -> None:
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass

    def _handle(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while self._running:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        self._reply(conn, {"ok": False, "error": "bad json"})
                        continue
                    self.last_activity = time.time()
                    cmd = msg.get("cmd")
                    if cmd == "ping":
                        self._reply(conn, {"ok": True, "ready": self.voice.ready.is_set(),
                                           "speaking": self.voice.speaking})
                    elif cmd == "say":
                        text = str(msg.get("text", ""))
                        style = str(msg.get("style", mood.DEFAULT_STYLE))
                        threading.Thread(target=self.voice.say, args=(text, style),
                                         daemon=True).start()
                        self._reply(conn, {"ok": True})
                    elif cmd == "stop":
                        self.voice.stop()
                        self._reply(conn, {"ok": True})
                    elif cmd == "quit":
                        self._reply(conn, {"ok": True})
                        self._running = False
                        return
                    else:
                        self._reply(conn, {"ok": False, "error": f"unknown {cmd!r}"})
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve(self) -> None:
        SOCK.parent.mkdir(parents=True, exist_ok=True)
        if SOCK.exists():
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.settimeout(1.0)
                probe.connect(str(SOCK))
                probe.close()
                return                          # another helper is alive
            except OSError:
                SOCK.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCK))
        os.chmod(SOCK, 0o600)
        srv.listen(8)
        srv.settimeout(1.0)
        self._srv = srv
        threading.Thread(target=self.voice.warm, daemon=True).start()
        log.info("speak helper listening on %s", SOCK)
        try:
            while self._running:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    idle = time.time() - self.last_activity
                    if idle > IDLE_EXIT_SECONDS and not self.voice.speaking:
                        log.info("idle for %ds - exiting to free memory", int(idle))
                        break
                    continue
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            srv.close()
            SOCK.unlink(missing_ok=True)


def _write_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    import wave
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    style = mood.DEFAULT_STYLE
    if "--style" in argv:
        style = argv[argv.index("--style") + 1]
    voice = ExpressiveVoice()
    if "--render" in argv:
        out = Path(argv[argv.index("--render") + 1])
        out.mkdir(parents=True, exist_ok=True)
        sr = voice.tts.sample_rate
        for name, text in SAMPLES:
            t0 = time.time()
            audio = np.concatenate([a for a, _ in voice.synth_plan(text, style)])
            dur = len(audio) / sr
            _write_wav(out / f"{style}-{name}.wav", audio, sr)
            print(f"{style:7} {name:11} {dur:5.1f}s audio  synth {time.time() - t0:4.1f}s")
        return 0
    if "--demo" in argv:
        for name, text in SAMPLES:
            print(f"[{name}] {text}")
            voice.say(text, style)
            time.sleep(0.2)
            voice.speaker.wait()
            time.sleep(0.4)
        return 0
    Server(voice).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
