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
    #: True if this hit came from the overlap seam at the head of a chunk, i.e. it is a
    #: re-transcription of audio the previous chunk already covered. Only seam hits may
    #: be deduped away — see `_dedupe`.
    seam: bool = False
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
        w0 = max(start, t - overlap)
        got = transcribe_window(video, w0, c1, model=model)
        for idx, w in enumerate(got):
            if not _matches(w.norm, targets):
                continue
            if w.probability < min_confidence:
                continue
            ctx = " ".join(x.text for x in got[max(0, idx - 4): idx + 5])
            # Remember which overlap region this hit came from. Only hits that could be
            # the *same utterance seen twice* — i.e. both inside the same overlap seam —
            # may be deduped against each other; see `_dedupe`.
            hits.append(Hit(w.norm, w.start, w.end, w.probability, ctx,
                            seam=(w.start < t) if t > start else False))
        if progress:
            pct = 100.0 * (c1 - start) / max(1e-9, end - start)
            print(f"  scan {c1:7.1f}/{end:.1f}s ({pct:5.1f}%)  {len(hits)} hits", flush=True)
        t = c1

    return _dedupe(hits)


#: How close two same-word hits must be to be treated as one utterance heard twice.
#: Deliberately shorter than a spoken syllable: rapid repetition ("fuck, fuck fuck")
#: puts real, distinct words ~0.3-0.5s apart, and a wider window silently deleted the
#: second and third from the scan entirely — they were never muted and never offered
#: for review, which read as the scanner "missing" words it had in fact discarded.
DEDUPE_TOL = 0.20


def _dedupe(hits: list[Hit], tol: float = DEDUPE_TOL) -> list[Hit]:
    """Collapse a hit heard twice across a chunk seam into one.

    Chunks overlap so a word straddling a boundary is not lost, which means the words
    inside the seam are transcribed twice. That duplication is the only thing this
    removes. Two genuinely distinct utterances of the same word, however close together,
    are both kept — collapsing those loses real content.
    """
    out: list[Hit] = []
    for h in sorted(hits, key=lambda x: (x.start, -x.confidence)):
        dup = any(
            p.word == h.word
            and abs(p.start - h.start) < tol
            # At least one side must be a seam re-transcription. Two hits from the same
            # pass over the same audio are distinct utterances by construction.
            and (p.seam or h.seam)
            for p in out
        )
        if dup:
            continue
        out.append(h)
    return out


#: Fraction of a hit that must fall inside a mute for the mute to own it.
#: Whisper's word boundaries move by tens of milliseconds between runs, and `tighten()`
#: trims a mute to the word's audible extent, so a hit is routinely a hair wider than
#: the mute covering it. Requiring full containment (the original rule) therefore
#: reported already-muted words as uncovered — and did so inconsistently from run to
#: run, which is what made the pending-review count oscillate instead of converging.
COVER_FRACTION = 0.5


def cross_reference(
    hits: list[Hit],
    mutes: list[tuple[str, float, float]],
    slack: float = 0.05,
) -> tuple[list[Hit], list[Hit]]:
    """Split scan hits into (covered by an existing mute, uncovered).

    `mutes` is (ref_id, start, end). Uncovered hits are the interesting output: words
    present in the audio that no planned mute touches.

    Coverage is by overlap, not containment: the question this answers is "is this word
    already being silenced?", and a mute that covers most of the word does silence it.
    """
    covered, missed = [], []
    for h in hits:
        span = max(1e-6, h.end - h.start)
        owner = None
        best = 0.0
        for rid, s, e in mutes:
            inter = min(h.end, e + slack) - max(h.start, s - slack)
            if inter <= 0:
                continue
            frac = inter / span
            if frac >= COVER_FRACTION and frac > best:
                owner, best = rid, frac
        if owner:
            h.covered_by = owner
            covered.append(h)
        else:
            missed.append(h)
    return covered, missed
