"""
Deciding how a reply should *sound*.

Kokoro has no emotion dial, so expressiveness comes from three things we do
control: which voice speaks a sentence, how fast, and the pause that follows.
This module picks those per sentence from what the sentence says - apologies
go soft and slow, good news goes brighter and quicker, questions get a lighter
touch - which is what makes a reply sound like a person reacting rather than a
reader reading.

Pure functions, no model: cheap to run and easy to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .tts import clean_for_speech, split_sentences


@dataclass(frozen=True)
class Delivery:
    """How one sentence is spoken."""
    mood: str
    voice: str
    speed: float
    pause: float          # seconds of silence after the sentence


# Voice styles: which Kokoro voice carries each mood. "bubbly" is the default
# because the brief was cute and human; "warm" and "soft" are calmer.
STYLES: dict[str, dict[str, tuple[str, float]]] = {
    "bubbly": {
        "neutral":    ("af_bella", 1.05),
        "cheerful":   ("af_bella", 1.12),
        "excited":    ("af_bella", 1.18),
        "curious":    ("af_bella", 1.04),
        "apologetic": ("af_heart", 0.92),
        "calm":       ("af_heart", 0.97),
    },
    "warm": {
        "neutral":    ("af_heart", 1.05),
        "cheerful":   ("af_heart", 1.12),
        "excited":    ("af_bella", 1.15),
        "curious":    ("af_heart", 1.02),
        "apologetic": ("af_nicole", 0.92),
        "calm":       ("af_heart", 0.96),
    },
    "soft": {
        "neutral":    ("af_nicole", 1.0),
        "cheerful":   ("af_heart", 1.08),
        "excited":    ("af_bella", 1.10),
        "curious":    ("af_nicole", 1.0),
        "apologetic": ("af_nicole", 0.90),
        "calm":       ("af_nicole", 0.95),
    },
}
DEFAULT_STYLE = "bubbly"

_APOLOGY = re.compile(
    r"\b(sorry|apolog\w+|unfortunately|couldn'?t|could not|can'?t|cannot|unable|"
    r"failed|didn'?t work|wasn'?t able|declined|not able|isn'?t available|"
    r"no luck|went wrong|error)\b", re.I)
_EXCITED = re.compile(
    r"\b(done|all set|accepted|solved|passed|success\w*|nailed|congrat\w+|great news|"
    r"awesome|amazing|perfect|fantastic|wonderful|got it|there you go|"
    r"ta-?da|hooray|yay)\b", re.I)
_CHEER = re.compile(
    r"\b(hi|hello|hey|good (morning|afternoon|evening)|welcome|thanks?|thank you|"
    r"you'?re welcome|of course|sure thing|happy to|glad|my pleasure|no problem|"
    r"love to|sounds good)\b", re.I)
_CALM = re.compile(
    r"\b(warning|careful|be aware|note that|important|irreversible|permanent|"
    r"cannot be undone|deleted)\b", re.I)


_PATH = re.compile(r"(?<![\w/])(?:~/|/)(?:[\w.\-]+/)*([\w.\-]+)")


def speakable(text: str) -> str:
    """Make text safe to read aloud: a long path becomes just its last part
    ("/Users/me/Documents/report.pdf" -> "report.pdf")."""
    return _PATH.sub(lambda m: m.group(1), text)


def classify(sentence: str) -> str:
    """The mood of one sentence."""
    s = sentence.strip()
    if not s:
        return "neutral"
    if _APOLOGY.search(s):
        return "apologetic"
    if _CALM.search(s):
        return "calm"
    if s.endswith("?"):
        return "curious"
    if _EXCITED.search(s) and ("!" in s or len(s) < 90):
        return "excited"
    if _CHEER.search(s) or s.endswith("!"):
        return "cheerful"
    return "neutral"


def _pause_after(sentence: str, last: bool) -> float:
    if last:
        return 0.0
    s = sentence.rstrip()
    if s.endswith("…") or s.endswith("..."):
        return 0.30
    if s.endswith("?"):
        return 0.22
    if s.endswith("!"):
        return 0.14
    if s.endswith(":"):
        return 0.20
    return 0.18


def plan(text: str, style: str = DEFAULT_STYLE) -> list[tuple[str, Delivery]]:
    """Split a reply into sentences, each with its own delivery."""
    table = STYLES.get(style, STYLES[DEFAULT_STYLE])
    spoken = speakable(clean_for_speech(text))
    sentences = split_sentences(spoken, min_len=18, first_min=8)
    out: list[tuple[str, Delivery]] = []
    carry_soft = False
    for i, sent in enumerate(sentences):
        mood = classify(sent)
        # After an apology the next neutral sentence stays gentle rather than
        # snapping back to chirpy, which sounds callous.
        if carry_soft and mood in ("neutral", "cheerful"):
            mood = "apologetic"
        carry_soft = mood == "apologetic"
        voice, speed = table[mood]
        out.append((sent, Delivery(mood, voice, speed,
                                   _pause_after(sent, i == len(sentences) - 1))))
    return out
