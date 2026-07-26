"""
Full-episode scan for target words.

Two uses, same machinery:

* **Discovery** — scan the SOURCE for target words. VidAngel's tagging is not
  guaranteed complete (and one tag in the sample set pointed at a word absent from
  the local file), so this is the only way to know what is actually in the audio.
* **Verification** — scan the OUTPUT after filtering and assert no target word
  survives.

Findings are **advisory**. Whisper misrecognises ("news" -> "noons") and hallucinates,
so auto-muting scan hits would introduce over-muting; reporting them cannot. A human
decides what gets added.
"""

from __future__ import annotations

from dataclasses import dataclass

from align import Word, get_model, probe_duration, transcribe_window
from locate import _matches, _variants


@dataclass
class Hit:
    word: str
    start: float
    end: float
    confidence: float
    context: str          # surrounding words, for judging false positives
    covered_by: str | None = None   # ref_id of the mute covering it, if any

    @property
    def timecode(self) -> str:
        m, s = divmod(self.start, 60)
        return f"{int(m):02d}:{s:06.3f}"


def scan(
    video: str,
    words: list[str],
    model=None,
    chunk: float = 120.0,
    overlap: float = 2.0,
    start: float = 0.0,
    end: float | None = None,
    min_confidence: float = 0.35,
    progress: bool = True,
) -> list[Hit]:
    """Transcribe the whole file in chunks and return every target-word hit.

    Chunks overlap so a word straddling a boundary is not lost; hits are deduped by
    proximity afterwards.
    """
    model = model or get_model()
    if end is None:
        end = probe_duration(video)

    targets: set[str] = set()
    for w in words:
        targets |= _variants(w)

    hits: list[Hit] = []
    t = start
    while t < end:
        c1 = min(end, t + chunk)
        got = transcribe_window(video, max(start, t - overlap), c1, model=model)
        for idx, w in enumerate(got):
            if not _matches(w.norm, targets):
                continue
            if w.probability < min_confidence:
                continue
            ctx = " ".join(x.text for x in got[max(0, idx - 4): idx + 5])
            hits.append(Hit(w.norm, w.start, w.end, w.probability, ctx))
        if progress:
            pct = 100.0 * (c1 - start) / max(1e-9, end - start)
            print(f"  scan {c1:7.1f}/{end:.1f}s ({pct:5.1f}%)  {len(hits)} hits", flush=True)
        t = c1

    return _dedupe(hits)


def _dedupe(hits: list[Hit], tol: float = 0.75) -> list[Hit]:
    """Collapse hits for the same word at nearly the same time (chunk overlap)."""
    out: list[Hit] = []
    for h in sorted(hits, key=lambda x: (x.start, -x.confidence)):
        if any(p.word == h.word and abs(p.start - h.start) < tol for p in out):
            continue
        out.append(h)
    return out


def cross_reference(
    hits: list[Hit],
    mutes: list[tuple[str, float, float]],
    slack: float = 0.05,
) -> tuple[list[Hit], list[Hit]]:
    """Split scan hits into (covered by an existing mute, uncovered).

    `mutes` is (ref_id, start, end). Uncovered hits are the interesting output: words
    present in the audio that no planned mute touches.
    """
    covered, missed = [], []
    for h in hits:
        owner = next(
            (rid for rid, s, e in mutes
             if h.start >= s - slack and h.end <= e + slack),
            None,
        )
        if owner:
            h.covered_by = owner
            covered.append(h)
        else:
            missed.append(h)
    return covered, missed
