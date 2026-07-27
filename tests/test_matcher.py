"""Word-matcher regression tests.

The matcher is the highest-risk component: a false positive mutes innocent dialogue, and
a false negative leaves a swear word audible. Both failure modes have actually happened,
so the cases below are drawn from real runs rather than invented.

Run: python tests/test_matcher.py
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"),
)

from locate import _matches, _variants  # noqa: E402

WORDS = ["damn", "hell", "ass", "bullshit", "god", "christ", "fuck", "shit",
         "bitch", "douche", "dick", "piss", "crap", "bastard",
         # Added after real tag-sets used these category keys.
         "cock", "jesus", "stupid", "prick", "screw", "suck", "arse", "pussy"]

SHOULD_MATCH = [
    "damn", "damned", "hell", "ass", "asses", "bullshit", "god", "christ",
    "fuck", "fucking", "fucked", "shit", "shits", "bitch", "bitches",
    "douche", "douches", "dick", "piss", "pissed", "pissing", "crap", "bastard",
]

SHOULD_NOT = [
    # Real false positives observed in a full-episode scan. "bullets" came from
    # prefix-stemming "bullshit"; "christmas" from "christ".
    "bullets", "christmas",
    # Short profanity hides inside ordinary vocabulary.
    "as", "assignment", "class", "pass", "glass", "grass", "assist", "assume",
    "hello", "shell", "shelter", "michelle", "gospel", "goddess", "good",
    "going", "gone", "christian", "bulletin", "bulldog", "bully", "dickens",
    "dictionary", "pistol", "scrap", "dame", "godfather", "asset", "brass",
    # Ordinary words that appeared adjacent to real mutes during testing.
    "the", "and", "paper", "real", "vigil", "candlelight", "partner", "news",
    # Innocent words containing a newer target. "sucker" was a real false positive:
    # the -er suffix rule matched it against "suck".
    "sucker", "suckers", "sucking", "cockpit", "cocktail", "peacock", "cocker",
    "screwdriver", "stupidity", "arsenal", "pussycat", "prickle",
]

SHOULD_MATCH_EXTRA = [
    "cock", "cocks", "jesus", "christ", "stupid", "prick", "screw", "screwed",
    "suck", "sucks", "arse", "pussy",
]


def main() -> int:
    targets: set[str] = set()
    for w in WORDS:
        targets |= _variants(w)

    failures = []
    for w in SHOULD_MATCH + SHOULD_MATCH_EXTRA:
        if not _matches(w, targets):
            failures.append(f"  MISS  {w!r} should match")
    for w in SHOULD_NOT:
        if _matches(w, targets):
            failures.append(f"  FALSE {w!r} should NOT match")

    print(f"{len(targets)} target spellings from {len(WORDS)} words")
    print(f"{len(SHOULD_MATCH)+len(SHOULD_MATCH_EXTRA)} must match, {len(SHOULD_NOT)} must not")
    if failures:
        print(f"\n{len(failures)} FAILURES:")
        print("\n".join(failures))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
