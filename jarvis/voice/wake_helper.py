"""
"Hey Jarvis" listener, run by the companion app as a small helper process.

The companion starts it (so it uses the app's microphone permission), reads
one line per detection from stdout - "WAKE 0.87" - and controls it through
stdin:

    pause    stop reacting (JARVIS is listening or speaking itself)
    resume   react again
    mic off  close the microphone (screen locked); "mic on" reopens it
    quit

Only the wake-word model sees the audio: ~1 ms of work per 80 ms frame, on
this Mac, nothing recorded and nothing sent anywhere.
"""
from __future__ import annotations

import queue
import sys
import threading
import time

import numpy as np

RATE = 16000
FRAME = 1280                     # 80 ms - openWakeWord's frame


def main() -> int:
    from jarvis.voice.wake import WakeWordDetector
    det = WakeWordDetector()
    if not det.load():
        print("ERROR wake model unavailable", flush=True)
        return 1
    import sounddevice as sd

    frames: queue.Queue = queue.Queue(maxsize=50)
    state = {"paused": False, "mic": True, "quit": False}
    stream_box: dict = {"s": None}

    def on_audio(indata, nframes, t, status):
        try:
            frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass                                   # never block the audio thread

    def open_mic() -> None:
        if stream_box["s"] is None:
            s = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                               blocksize=FRAME, callback=on_audio)
            s.start()
            stream_box["s"] = s

    def close_mic() -> None:
        s = stream_box["s"]
        stream_box["s"] = None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    def commands() -> None:
        for line in sys.stdin:
            cmd = line.strip().lower()
            if cmd == "pause":
                state["paused"] = True
            elif cmd == "resume":
                det.reset()                        # forget audio heard while paused
                state["paused"] = False
            elif cmd == "mic off":
                state["mic"] = False
            elif cmd == "mic on":
                state["mic"] = True
            elif cmd == "quit":
                break
        state["quit"] = True                       # stdin closed: the app is gone

    threading.Thread(target=commands, daemon=True).start()
    open_mic()
    print("READY", flush=True)
    while not state["quit"]:
        if not state["mic"]:
            close_mic()
            time.sleep(0.3)
            continue
        if stream_box["s"] is None:
            try:
                open_mic()
            except Exception as e:                 # noqa: BLE001
                print(f"ERROR mic: {e}", flush=True)
                time.sleep(3)
                continue
        try:
            f = frames.get(timeout=0.5)
        except queue.Empty:
            continue
        if state["paused"]:
            continue
        if det.feed(f):
            print(f"WAKE {det.last_score:.2f}", flush=True)
    close_mic()
    return 0


if __name__ == "__main__":
    sys.exit(main())
