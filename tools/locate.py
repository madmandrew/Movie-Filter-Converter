"""
Turn an approximate incident (`start_approx`, `end_approx`, expected phrase) into
exact, frame-snapped mute boundaries.

Strategy: widen the estimate into a search window, transcribe it, find the expected
word(s), then refine the edges against audio energy — Whisper stretches word spans
to fill silence, so its boundaries are outer bounds rather than tight ones.
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass

import numpy as np

from align import Word, _tool, extract_audio, transcribe_window

# Profanity Whisper tends to sanitise, mapped to what it actually emits. Used to
# match the transcript against VidAngel's expected phrase.
_SOFTENED = {
    "fuck": ["frick", "freak", "duck"],
    "fucking": ["fricking", "freaking", "ducking"],
    "shit": ["shoot", "sheet"],
    "bitch": ["beach"],
    "damn": ["dam", "darn"],
    "goddamn": ["goddam"],
    "hell": ["heck"],
    "bastard": ["basted"],
    # NOTE: deliberately no "as" for "ass". The preposition "as" is one of the most
    # common words in English ("as it unfolds", "as a group") and mapping it here
    # produced 5 false hits per episode. Same reasoning for dropping "f" -> "fuck"
    # and "ship" -> "shit": the cure is worse than the miss.
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


#: Short targets that are substrings of common innocent words. For these, only an
#: exact match counts — "assignment"/"class"/"hello"/"shell" must never be muted.
_EXACT_ONLY = {"ass", "hell", "damn", "god", "dick", "piss", "crap"}

#: Legitimate words that contain a target as a substring; never match these.
_FALSE_FRIENDS = {
    "class", "classes", "pass", "passed", "passing", "assignment", "assign",
    "assist", "assume", "assumed", "glass", "grass", "mass", "bass", "brass",
    "embarrass", "harassment", "cassette", "assembly", "asset", "hello", "shell",
    "shelter", "hellman", "damned",  # "damned" is handled via its own variant
    "goddess", "gospel", "gone", "going", "good", "dickens", "dictionary",
}


def _variants(word: str) -> set[str]:
    n = _norm(word)
    out = {n}
    out.update(_SOFTENED.get(n, []))
    # Whisper often drops the -ing/-ed inflection or emits a stem. Only safe for
    # longer words; stemming "hell" or "ass" would match far too much.
    if len(n) > 5 and n not in _EXACT_ONLY:
        out.add(n[:4])
    return out


#: Never match these, whatever the target list says — all are extremely common words
#: that a lenient profanity matcher would otherwise flag.
_NEVER = {"as", "is", "at", "us", "it", "an", "a", "he", "she", "gas", "has", "was"}


def _matches(word_norm: str, targets: set[str]) -> bool:
    """Does a transcript word match any target?

    Short profanity is a substring of ordinary vocabulary ("ass" in "assignment",
    "hell" in "hello"), so prefix/substring matching produces false mutes on innocent
    dialogue. Those targets require an exact hit.
    """
    if not word_norm or word_norm in _FALSE_FRIENDS or word_norm in _NEVER:
        return False
    if word_norm in targets:
        return True
    for t in targets:
        if not t or t in _EXACT_ONLY:
            continue                      # exact match only, already checked above
        if len(word_norm) < 3:
            continue
        # Allow inflection either direction ("fucking" vs "fuck") but require a
        # substantial shared prefix so unrelated words cannot collide.
        if (word_norm.startswith(t) or t.startswith(word_norm)) and min(len(t), len(word_norm)) >= 4:
            return True
    return False


@dataclass
class Match:
    """A located incident. Times are absolute seconds into the video."""
    expected: str
    start: float
    end: float
    matched_text: str
    confidence: float
    approx_start: float
    approx_end: float
    method: str                  # how the boundary was found
    drift: float                 # located start minus approximate start

    @property
    def duration(self) -> float:
        return self.end - self.start


def _load_pcm(video: str, start: float, end: float) -> tuple[np.ndarray, int]:
    """Mono 16 kHz float samples for a window."""
    wav = extract_audio(video, start, end)
    try:
        import wave

        with wave.open(wav, "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return pcm, sr
    finally:
        import os

        try:
            os.unlink(wav)
        except OSError:
            pass


def refine_edges(
    video: str,
    start: float,
    end: float,
    floor_ratio: float = 0.12,
    pad: float = 0.0,
) -> tuple[float, float, str]:
    """Tighten [start, end] onto actual speech energy.

    Whisper's word spans absorb surrounding silence. We look at RMS energy in 10 ms
    frames and walk inward to where the signal first/last exceeds a fraction of the
    window peak. Returns (start, end, method).
    """
    ctx = 0.35
    w_start = max(0.0, start - ctx)
    pcm, sr = _load_pcm(video, w_start, end + ctx)
    if pcm.size == 0:
        return start, end, "energy-empty"

    hop = max(1, int(0.010 * sr))
    frames = pcm[: (pcm.size // hop) * hop].reshape(-1, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)

    # Only consider the nominal word region when picking the threshold, so a loud
    # neighbouring word doesn't raise the floor past our quiet target word.
    i0 = int((start - w_start) / 0.010)
    i1 = int((end - w_start) / 0.010)
    i0, i1 = max(0, i0), min(len(rms), max(i0 + 1, i1))
    core = rms[i0:i1]
    if core.size == 0:
        return start, end, "energy-empty"

    thresh = max(core.max() * floor_ratio, rms.min() * 3.0)
    above = np.where(core >= thresh)[0]
    if above.size == 0:
        return start, end, "energy-nosignal"

    new_start = w_start + (i0 + above[0]) * 0.010
    new_end = w_start + (i0 + above[-1] + 1) * 0.010
    return max(0.0, new_start - pad), new_end + pad, "energy"


def snap_to_frames(start: float, end: float, fps: float) -> tuple[float, float]:
    """Expand outward to whole frame boundaries.

    At 23.976 fps frames do not land on whole seconds (frame 24 = 1.001s). Always
    round outward so a mute never lands mid-word.
    """
    return math.floor(start * fps) / fps, math.ceil(end * fps) / fps


def locate(
    video: str,
    expected: str,
    approx_start: float,
    approx_end: float,
    fps: float,
    model=None,
    search_pad: float = 2.5,
    mute_pad: float = 0.06,
    refine: bool = True,
) -> Match | None:
    """Find `expected` near the approximate window; None if not found.

    `search_pad` must exceed the worst expected drift. VidAngel's integer-second
    rounding is ~1s, but a filter keyed to a different master can be 12s+ out — pass
    a larger pad (or a pre-computed global offset) for those.
    """
    w0 = max(0.0, approx_start - search_pad)
    w1 = approx_end + search_pad
    words = transcribe_window(video, w0, w1, model=model, hotwords=expected)
    if not words:
        return None

    targets = set()
    for tok in expected.split():
        targets |= _variants(tok)

    hits = [w for w in words if _matches(w.norm, targets)]
    if not hits:
        return None

    # Prefer the hit closest to where VidAngel said it was.
    mid = (approx_start + approx_end) / 2.0
    best = min(hits, key=lambda w: abs((w.start + w.end) / 2.0 - mid))

    # Merge adjacent hits (multi-word phrases like "god damn").
    group = [best]
    for w in hits:
        if w is not best and abs(w.start - group[-1].end) < 0.30:
            group.append(w)
    group.sort(key=lambda w: w.start)
    s, e = group[0].start, group[-1].end

    method = "whisper"
    if refine:
        s, e, method = refine_edges(video, s, e)

    s = max(0.0, s - mute_pad)
    e = e + mute_pad
    s, e = snap_to_frames(s, e, fps)

    return Match(
        expected=expected,
        start=s,
        end=e,
        matched_text=" ".join(w.text for w in group),
        confidence=min(w.probability for w in group),
        approx_start=approx_start,
        approx_end=approx_end,
        method=method,
        drift=s - approx_start,
    )
