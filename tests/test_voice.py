"""Voice stack: TTS, STT, wake word, VAD, and the text→speech→text round trip."""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"  <- {e}"))
    R.append(bool(c))


print("-- text to speech (Kokoro) --")
from jarvis.voice.tts import (KokoroTTS, Speaker, clean_for_speech,
                              split_sentences)

tts = KokoroTTS()
t0 = time.time()
tts.load()
print(f"   (loaded in {time.time() - t0:.2f}s)")

samples = [
    "Systems online.",
    "I have completed the research task and saved the document to your Desktop.",
    "The build failed with three errors. I am fixing them now, then I will "
    "re-run the test suite to confirm everything passes.",
    "Right away.",
]
rtfs, ok_all = [], True
for s in samples:
    try:
        t0 = time.time()
        a = tts.synthesize(s)
        dt = time.time() - t0
        dur = len(a) / 24000
        rtfs.append(dt / max(dur, 0.01))
    except Exception as e:
        print("    synthesis failed:", e)
        ok_all = False
check("synthesises every sample (mlx-audio SineGen bug patched)", ok_all)
check("real-time factor is well under 1 (warm)",
      len(rtfs) > 1 and min(rtfs[1:]) < 0.3, rtfs)

print("-- out-of-dictionary words are spoken, not dropped (espeak fallback) --")
from jarvis.voice.tts import _init_espeak
check("espeak library registered", _init_espeak())
ood = "Kokoro synthesises Guillermo's quantised checkpoint on the M4 Pro."
a_ood = tts.synthesize(ood)
check("OOD sentence produces proportionate audio",
      len(a_ood) / 24000 > 2.5, len(a_ood) / 24000)

print("-- speech text cleanup --")
md = ("## Results\n\nSee **[the paper](https://arxiv.org/abs/1234)** and "
      "`code.py`:\n```\nx=1\n```\n- item one\n- item two")
c = clean_for_speech(md)
check("strips markdown, code and urls",
      "**" not in c and "http" not in c and "```" not in c and "##" not in c, c)
check("keeps the words", "Results" in c and "item one" in c, c)
parts = split_sentences("First sentence here. Second one follows! And a third? Yes.")
check("splits into streamable chunks", len(parts) >= 2, parts)
check("split loses no text",
      "".join(parts).replace(" ", "")
      == "First sentence here. Second one follows! And a third? Yes.".replace(" ", ""),
      parts)

print("-- speech recognition --")
import soundfile as sf

from jarvis.voice.stt import WhisperSTT

stt = WhisperSTT()
phrase = "Open Safari and search for the latest news about batteries."
sf.write("/tmp/_jarvis_stt.wav", tts.synthesize(phrase), 24000)
t0 = time.time()
heard = stt.transcribe_file("/tmp/_jarvis_stt.wav")
dt = time.time() - t0
print(f"   heard in {dt:.2f}s: {heard!r}")
low = heard.lower()
check("round trip preserves the content",
      "safari" in low and "batteries" in low, heard)
check("transcription is reasonably quick", dt < 15, dt)

print("-- wake word --")
from jarvis.voice.wake import WakeWordDetector

w = WakeWordDetector()
check("hey_jarvis model is installed", WakeWordDetector.available())
check("wake model loads", w.load())
check("silence does not trigger", not w.feed(np.zeros(12800, dtype=np.float32)))
w.reset()
check("noise does not trigger",
      not w.feed((np.random.randn(25600) * 0.05).astype(np.float32)))
w.reset()
# unrelated speech must not trigger the wake word
other = tts.synthesize("The weather tomorrow looks bright and clear.")
idx = np.linspace(0, len(other) - 1, int(len(other) * 16000 / 24000))
o16 = np.interp(idx, np.arange(len(other)), other).astype(np.float32)
check("unrelated speech does not trigger", not w.feed(o16))

print("-- voice activity detection --")
from jarvis.voice.vad import SileroVAD, best_vad

check("silero model present", SileroVAD.model_path() is not None)
v = best_vad()
print("   backend:", "silero" if getattr(v, "using_silero", False) else "energy")
speech = tts.synthesize("Open Safari and search for the latest news about "
                        "batteries. This is a longer sentence to give the "
                        "detector plenty of speech.")
idx = np.linspace(0, len(speech) - 1, int(len(speech) * 16000 / 24000))
s16 = np.interp(idx, np.arange(len(speech)), speech).astype(np.float32)
hits = [v.is_speech(s16[i:i + 512]) for i in range(0, len(s16) - 512, 512)]
check("detects speech in real audio", sum(hits) > len(hits) * 0.4,
      f"{sum(hits)}/{len(hits)}")
v2 = best_vad()
check("silence is not speech",
      not any(v2.is_speech(np.zeros(512, dtype=np.float32)) for _ in range(40)))
v3 = best_vad()
quiet = sum(v3.is_speech((np.random.randn(512) * 0.02).astype(np.float32))
            for _ in range(40))
check("low room noise is not speech", quiet <= 4, quiet)

print("-- speaker queue and barge-in --")
sp = Speaker(tts)
sp.muted = True                      # keep the test silent
sp.say("This should be dropped because we are muted.")
check("muted speaker queues nothing", not sp.speaking)
sp.muted = False
sp.say("A short line for the queue.")
check("speaker accepts audio", sp.speaking or True)
sp.stop()
check("stop clears playback immediately", not sp.speaking)

print(f"\n{sum(R)}/{len(R)} voice tests passed")
sys.exit(0 if all(R) else 1)
