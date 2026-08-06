"""The corroboration check that stops hotword-invented words becoming mutes.

`locate()` passes `hotwords=expected` to Whisper, which is necessary — Whisper sanitises
profanity and recall collapses without the bias. But on a stretch with no speech the same
bias manufactures the word. Measured on the Severance archives:

    S01E07 title sequence   hotwords='fucker' -> 'fucker' p=0.004
                            no hotwords       -> no words at all
    S01E07 @392.85s         hotwords='fucker' -> 'fucker' p=0.003
                            no hotwords       -> 'You'    p=0.067

That put mutes over the opening titles, which have no dialogue in them at all.

The obvious fix — a confidence floor on the biased decode — is WRONG, and this file exists
mostly to record why. Biasing deflates a genuine hit's score just as it inflates an
invented one:

    S02E03 @2350.7s ("Fuck.")   hotwords='fuck' -> p=0.118   no hotwords -> p=0.759
    S01E07 @311.35s (real)      hotwords='fuck' -> p=0.847   no hotwords -> p=0.962

A floor at 0.25 on the biased score would have discarded the first of those, which is
plainly audible. So the check is corroboration against a second, UNBIASED decode of the
same window, with the floor applied there — the rule `verify` already used for deciding
whether a word is still audible, just never applied when *finding* one.

The two decodes can disagree in either direction, so both are allowed to contribute:

    S02E03 @1514.9s ("Shit.")   biased -> merged into a long p=0.001 'look', no target
                                honest -> 'shit' p=0.583

Run: python tests/test_hallucination_floor.py
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"),
)

from locate import HALLUCINATION_P, _matches, _variants  # noqa: E402
import verify as _verify  # noqa: E402


class _W:
    """Stand-in for align.Word — only the fields the hit filter reads."""

    def __init__(self, norm, probability, start=0.0, end=0.5):
        self.norm = norm
        self.probability = probability
        self.start = start
        self.end = end


def _decide(biased, honest, expected):
    """The selection locate() performs, minus the ffmpeg/Whisper calls.

    Mirrors the real code: corroborate against the honest decode, keep the biased
    decode's boundaries where both agree, fall back to the honest decode alone.
    """
    targets = set()
    for tok in expected.split():
        targets |= _variants(tok)

    hits = [w for w in biased if _matches(w.norm, targets)]
    corroborated = [w for w in honest
                    if _matches(w.norm, targets) and w.probability >= HALLUCINATION_P]
    if not corroborated:
        return []
    agreed = [w for w in hits
              if any(abs(w.start - c.start) <= 0.5 or abs(w.end - c.end) <= 0.5
                     for c in corroborated)]
    return agreed or corroborated


CASES = [
    (
        "S01E07 title sequence: invented from music",
        "fucker", [_W("fucker", 0.004)], [], False,
    ),
    (
        "S01E07 @392.85s: 'You' rewritten as 'fucker'",
        "fucker", [_W("fucker", 0.003)], [_W("you", 0.067)], False,
    ),
    (
        "real hit, both decodes agree",
        "fuck", [_W("fuck", 0.847)], [_W("fuck", 0.962)], True,
    ),
    (
        "S02E03 @2350.7s: biased score p=0.118, honest p=0.759",
        "fuck", [_W("fuck", 0.118)], [_W("fuck", 0.759)], True,
    ),
    (
        "S02E03 @1514.9s: biased decode misses it entirely",
        "shit", [_W("look", 0.001)], [_W("shit", 0.583)], True,
    ),
    (
        "honest decode hears it, but only as a hallucination-grade emission",
        "shit", [_W("shit", 0.900)], [_W("shit", 0.05)], False,
    ),
    (
        "corroborating word is a different utterance 4s away",
        "fuck", [_W("fuck", 0.90, start=10.0, end=10.4)],
        [_W("fuck", 0.90, start=14.0, end=14.4)], True,
    ),
]


def main():
    failures = 0
    print(f"floor on the HONEST decode: p >= {HALLUCINATION_P}\n")
    for label, word, biased, honest, want in CASES:
        got = bool(_decide(biased, honest, word))
        ok = got == want
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {'kept   ' if got else 'dropped'}  {label}")

    # A non-target word is rejected however confident either decode is.
    if _decide([_W("hello", 0.99)], [_W("hello", 0.99)], "hell"):
        print("  FAIL  'hello' matched target 'hell'")
        failures += 1
    else:
        print("  PASS  dropped  non-target stays rejected")

    # The two modules must agree; verify re-exports the constant rather than copying it.
    if _verify._HALLUCINATION_P != HALLUCINATION_P:
        print("  FAIL  verify._HALLUCINATION_P has drifted from locate.HALLUCINATION_P")
        failures += 1
    else:
        print("  PASS  verify and locate share one constant")

    print("\n" + ("ALL PASS" if not failures else f"{failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
