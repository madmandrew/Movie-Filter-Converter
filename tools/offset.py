"""
Estimate the offset between a filter source's timeline and the local file.

Filter sources (VidAngel, VideoSkip) are keyed to whatever cut the provider had. A local
rip can differ by seconds to minutes — a different master, an extra distributor card, a
longer or shorter edit. Any fixed search window eventually loses that race.

The fix falls out of work already being done: the full-episode scan transcribes the whole
track anyway, so the true position of every target word is already known. Comparing those
against the source's claimed positions yields the offset directly — no alignment pass, no
audio fingerprinting, no extra GPU time.

Method: for each source tag, find scan hits of the same word and record the time
differences. The correct offset appears as a *cluster* in that set, while coincidental
pairings scatter. The densest cluster wins.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OffsetEstimate:
    offset: float          # add to source times to get local times
    support: int           # tags agreeing with this offset
    considered: int        # tags that had any candidate at all
    spread: float          # std deviation within the cluster, seconds
    confident: bool

    @property
    def summary(self) -> str:
        if not self.confident:
            return (f"no reliable offset ({self.support}/{self.considered} tags agreed) "
                    f"— treating the source timeline as correct")
        return (f"offset {self.offset:+.2f}s from {self.support}/{self.considered} tags "
                f"(spread {self.spread:.2f}s)")


#: A cluster this tight is accepted on two tags alone. Rationale, measured on a real
#: episode (For All Mankind S01E02, run 31): two tags 48 minutes apart, for *different*
#: words, agreed on -34.98s to within 0.18s. For a coincidental pairing to do that, two
#: deltas free to fall anywhere in ±`max_offset` must land within 0.18s of each other —
#: p ≈ 0.0006, i.e. the tight pair is ~1600x likelier to be real than chance. Requiring
#: a third tag there discarded a correct offset and sent all six incidents into a ±10s
#: search 35s away from the word, which reported NOT_FOUND for every one of them.
TIGHT_SPREAD = 0.5

#: Minimum tags in a tight cluster. Two points far apart pin a constant offset; one
#: cannot be distinguished from a single mistimed tag.
TIGHT_MIN_SUPPORT = 2

#: How far apart the supporting tags must be before two of them count as proof. Two
#: hits from the same scene could both be wrong in the same direction; two an episode
#: apart agreeing to a fraction of a second could not.
TIGHT_MIN_BASELINE = 60.0

#: Distinct words the tight cluster must draw on. One word tagged M times and heard N
#: times yields M*N deltas that are not independent evidence — a coincidental near-tie
#: among them is likely, not remarkable. Two different words agreeing is not reachable
#: by that route. The For All Mankind case the tight rule was built for already had two.
TIGHT_MIN_WORDS = 2


def estimate(
    expected: list[tuple[float, str]],
    observed: list[tuple[float, str]],
    max_offset: float = 300.0,
    tolerance: float = 1.5,
    min_support: int = 3,
) -> OffsetEstimate:
    """Estimate a constant offset between `expected` and `observed` (time, word) pairs.

    `expected` comes from the filter source, `observed` from scanning the local file.
    `tolerance` is how far apart two deltas may be and still count as the same offset;
    `max_offset` bounds the search so an unrelated file cannot produce a spurious match.

    Returns an estimate with `confident=False` rather than guessing when support is thin —
    a wrong offset is far worse than none, since it would move every mute.
    """
    # (delta, source time, word) — the source time is kept so the cluster's baseline can
    # be measured, the word so the tight rule can tell independent evidence from many
    # pairings of one repeated word. Support count alone cannot tell two agreeing points
    # in one scene from two an episode apart, and only the latter pins a constant offset.
    deltas: list[tuple[float, float, str]] = []
    considered = 0

    for want_t, word in expected:
        w = (word or "").lower()
        if not w:
            continue
        cands = [(obs_t - want_t, want_t, w) for obs_t, obs_w in observed
                 if obs_w.lower() == w and abs(obs_t - want_t) <= max_offset]
        if cands:
            considered += 1
            deltas.extend(cands)

    if not deltas:
        return OffsetEstimate(0.0, 0, considered, 0.0, False)

    # Densest cluster: for each delta, count neighbours within tolerance.
    deltas.sort()
    best_members: list[tuple[float, float, str]] = []
    for d, _t, _w in deltas:
        members = [(x, t, w) for x, t, w in deltas if abs(x - d) <= tolerance]
        if len(members) > len(best_members):
            best_members = members

    n = len(best_members)
    vals = [x for x, _t, _w in best_members]
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / n
    spread = var ** 0.5

    src_times = [t for _x, t, _w in best_members]
    baseline = max(src_times) - min(src_times)
    distinct_words = len({w for _x, _t, w in best_members})

    # Two independent routes to confidence.
    #
    # The original rule — enough tags, and a majority of those that had any candidate.
    broad = n >= min_support and n >= max(1, considered) * 0.5
    # Or a cluster so tight, and spanning so much of the runtime, that coincidence is
    # not a credible explanation. This exists because the broad rule is unreachable on
    # an episode where most tags name words the transcript never produced: support is
    # capped by how many tags could vote at all, not by how right the answer is.
    #
    # The distinct-word requirement bounds the coincidence argument. A word tagged M
    # times and heard N times contributes M*N candidate deltas, all free to fall
    # anywhere in ±max_offset, so the chance that *some* pair of them lands within
    # TIGHT_SPREAD grows with the square of how common the word is — the p ≈ 0.0006
    # figure above holds for one pairing, not for the ~100 that a heavily-repeated word
    # generates. Measured: Severance S01E02 (run 41) accepted -95.05s on 5/12 tags from
    # repeated pairings of one word, against a -52s runtime delta; the offset pushed a
    # 54s tag to -41s and the run died. Two *different* words agreeing cannot be
    # manufactured that way.
    tight = (n >= TIGHT_MIN_SUPPORT and spread <= TIGHT_SPREAD
             and baseline >= TIGHT_MIN_BASELINE
             and distinct_words >= TIGHT_MIN_WORDS)

    return OffsetEstimate(offset=mean, support=n, considered=considered,
                          spread=spread, confident=broad or tight)


def apply_offset(times: list[float], offset: float) -> list[float]:
    return [max(0.0, t + offset) for t in times]
