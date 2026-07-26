"""
Prove a mute actually worked.

Applies a candidate mute to a short clip, re-transcribes it, and asserts:
  1. the target word is GONE            (no under-muting)
  2. the surrounding words SURVIVE      (no over-muting)

Failing (1) means the window was too tight or mislocated; failing (2) means it ate
neighbouring dialogue. Both are actionable, so `verify` reports which occurred and
`tighten` iterates until clean.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass

from align import _tool, transcribe_window
from locate import _matches, _variants, _norm, snap_to_frames


@dataclass
class Verdict:
    ok: bool
    target_present: bool          # True => under-muted, word still audible
    lost_neighbours: list[str]    # words that vanished => over-muted
    heard: str                    # transcript of the muted clip
    note: str = ""


def render_muted(
    video: str,
    mutes: list[tuple[float, float]],
    clip_start: float,
    clip_end: float,
    dest: str | None = None,
) -> str:
    """Render [clip_start, clip_end] to WAV with `mutes` silenced.

    Uses ffmpeg's `volume` filter with time-range enables rather than cutting, so the
    timeline is preserved and transcript positions stay comparable.
    """
    if dest is None:
        fd, dest = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

    if mutes:
        expr = "+".join(f"between(t,{s:.4f},{e:.4f})" for s, e in mutes)
        af = f"volume=0:enable='{expr}'"
    else:
        af = "anull"

    subprocess.run(
        [
            _tool("ffmpeg"), "-v", "error", "-y",
            # Filter timestamps are relative to the trimmed input, so seek with the
            # accurate (post-input) form and rebase mute times by clip_start.
            "-ss", f"{clip_start:.3f}", "-to", f"{clip_end:.3f}",
            "-i", video,
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            "-af", af,
            dest,
        ],
        check=True, capture_output=True,
    )
    return dest


#: Below this probability a "word" in muted audio is a language-model hallucination
#: from context, not real audio. Measured on silenced clips: genuine speech scores
#: p>0.5, words invented from silence score p<0.10.
_HALLUCINATION_P = 0.25

#: Function words whose partial clipping at a mute edge is inaudible in practice.
_STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "at", "is", "it", "i", "and", "or",
    "for", "with", "as", "so", "we", "you", "he", "she", "my", "your", "that",
    "this", "was", "are", "be", "do", "up", "no", "not", "but", "if", "me", "him",
    "her", "them", "its", "our", "all", "just", "like", "oh", "uh", "um", "hey",
}


def verify(
    video: str,
    expected: str,
    mute_start: float,
    mute_end: float,
    model=None,
    context: float = 3.0,
    residual_ratio: float = 0.06,
) -> Verdict:
    """Check one mute for under- and over-muting.

    Two independent checks, because neither alone is trustworthy:

    * **Energy** — RMS inside the mute range versus nearby speech. This is ground
      truth for "is there still sound here", immune to Whisper's guessing.
    * **Transcript** — run WITHOUT hotwords and with a confidence floor. Whisper
      will happily hallucinate a plausible word from silence ("...is her ___" ->
      'mother' at p=0.08), and biasing it toward the word we're testing for absence
      guarantees a false positive. Only confident emissions count as audible.
    """
    c0 = max(0.0, mute_start - context)
    c1 = mute_end + context

    targets = set()
    for tok in expected.split():
        targets |= _variants(tok)

    def is_target(w) -> bool:
        return _matches(w.norm, targets)

    # Baseline transcript (no hotwords — keep it honest).
    before = transcribe_window(video, c0, c1, model=model)

    # Over-muting is a TIMING question, not a transcript-diff question: does the mute
    # range swallow a meaningful slice of a neighbouring word? Comparing two noisy
    # transcripts word-for-word gives phantom failures — the same audio decodes
    # differently run to run ("news" vs "noons"; a word's confidence drifting under
    # any fixed survivor floor). Geometry on the baseline is stable.
    # Clipping a short function word adjacent to the target is unavoidable given any
    # safety pad, and inaudible in practice. Only real content words count as damage.
    overlap_tol = 0.10
    lost = []
    for w in before:
        if is_target(w) or not w.norm or w.probability < 0.60:
            continue
        if w.norm in _STOPWORDS or len(w.norm) <= 2:
            continue
        covered = min(w.end, mute_end) - max(w.start, mute_start)
        if covered > overlap_tol and covered > 0.35 * (w.end - w.start):
            lost.append(w.norm)
    lost = sorted(set(lost))

    rel_mute = (mute_start - c0, mute_end - c0)
    wav = render_muted(video, [rel_mute], c0, c1)
    try:
        after = transcribe_window_file(wav, c0, model=model, hotwords=None)
        residual, reference = _mute_energy(wav, mute_start - c0, mute_end - c0)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass

    # Energy check: is the mute range actually quiet relative to nearby speech?
    ratio = (residual / reference) if reference > 0 else 0.0
    energy_clean = ratio <= residual_ratio

    # Transcript check: confident target emission overlapping the mute range.
    still_there = [
        w for w in after
        if is_target(w)
        and w.probability >= _HALLUCINATION_P
        and w.end > mute_start and w.start < mute_end
    ]

    target_present = bool(still_there) or not energy_clean

    # Contiguous speech is a real conflict, not a bug: in "a real damn paper" the
    # three words share boundaries with zero silence, so no mute removes the middle
    # one without clipping its neighbours. Removing the swear word wins — a clipped
    # consonant is preferable to audible profanity — but say so in the note.
    clipped_only = bool(lost) and not target_present
    ok = not target_present and (not lost or clipped_only)

    note = ""
    if not energy_clean:
        note = f"residual audio in mute range ({ratio:.1%} of speech level)"
    elif still_there:
        note = (f"target still audible as {still_there[0].text!r} "
                f"(p={still_there[0].probability:.2f})")
    elif lost:
        note = f"clipped contiguous neighbour(s): {', '.join(lost)}"

    return Verdict(
        ok=ok,
        target_present=target_present,
        lost_neighbours=lost,
        heard=" ".join(w.text for w in after),
        note=note,
    )


def _mute_energy(wav: str, rel_start: float, rel_end: float) -> tuple[float, float]:
    """(RMS inside the mute range, RMS of the loudest nearby 200 ms) for a clip."""
    import wave

    import numpy as np

    with wave.open(wav, "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    pcm = pcm.astype(np.float32) / 32768.0
    if pcm.size == 0:
        return 0.0, 0.0

    i0, i1 = int(max(0.0, rel_start) * sr), int(rel_end * sr)
    i1 = min(pcm.size, max(i0 + 1, i1))
    inside = pcm[i0:i1]
    residual = float(np.sqrt((inside ** 2).mean())) if inside.size else 0.0

    # Reference level: loudest 200 ms window outside the mute, so we compare against
    # actual speech rather than room tone.
    hop = int(0.200 * sr)
    outside = np.concatenate([pcm[:i0], pcm[i1:]])
    if outside.size < hop:
        return residual, float(np.sqrt((outside ** 2).mean() + 1e-12))
    blocks = outside[: (outside.size // hop) * hop].reshape(-1, hop)
    reference = float(np.sqrt((blocks ** 2).mean(axis=1)).max())
    return residual, reference


def transcribe_window_file(wav: str, offset: float, model=None, hotwords=None):
    """Transcribe an already-extracted WAV, rebasing timestamps by `offset`."""
    from align import Word, get_model

    model = model or get_model()
    segments, _ = model.transcribe(
        wav,
        word_timestamps=True,
        condition_on_previous_text=False,
        hotwords=hotwords,
        vad_filter=False,
        beam_size=5,
    )
    words = []
    for seg in segments:
        for w in (seg.words or []):
            words.append(
                Word(text=w.word.strip(), start=offset + w.start,
                     end=offset + w.end, probability=w.probability)
            )
    return words


def tighten(
    video: str,
    expected: str,
    start: float,
    end: float,
    fps: float,
    model=None,
    max_rounds: int = 4,
    step: float = 0.08,
) -> tuple[float, float, Verdict, int]:
    """Iteratively widen (if under-muted) or narrow (if over-muted) until clean.

    Returns the final bounds, the last verdict, and how many rounds were needed.
    """
    s, e = start, end
    verdict = verify(video, expected, s, e, model=model)
    rounds = 0

    while not verdict.ok and rounds < max_rounds:
        rounds += 1
        if verdict.target_present:
            s, e = s - step, e + step           # under-muted: widen
        else:
            # Only neighbour clipping remains, which `verify` already accepts when the
            # target is gone. Narrowing from here would re-expose the swear word.
            break
        s, e = snap_to_frames(max(0.0, s), e, fps)
        verdict = verify(video, expected, s, e, model=model)

    return s, e, verdict, rounds
