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
    # (delta, source time) — the source time is kept so the cluster's baseline can be
    # measured. Support count alone cannot tell two agreeing points in one scene from
    # two an episode apart, and only the latter pins a constant offset.
    deltas: list[tuple[float, float]] = []
    considered = 0

    for want_t, word in expected:
        w = (word or "").lower()
        if not w:
            continue
        cands = [(obs_t - want_t, want_t) for obs_t, obs_w in observed
                 if obs_w.lower() == w and abs(obs_t - want_t) <= max_offset]
        if cands:
            considered += 1
            deltas.extend(cands)

    if not deltas:
        return OffsetEstimate(0.0, 0, considered, 0.0, False)

    # Densest cluster: for each delta, count neighbours within tolerance.
    deltas.sort()
    best_members: list[tuple[float, float]] = []
    for d, _t in deltas:
        members = [(x, t) for x, t in deltas if abs(x - d) <= tolerance]
        if len(members) > len(best_members):
            best_members = members

    n = len(best_members)
    vals = [x for x, _t in best_members]
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / n
    spread = var ** 0.5

    src_times = [t for _x, t in best_members]
    baseline = max(src_times) - min(src_times)

    # Two independent routes to confidence.
    #
    # The original rule — enough tags, and a majority of those that had any candidate.
    broad = n >= min_support and n >= max(1, considered) * 0.5
    # Or a cluster so tight, and spanning so much of the runtime, that coincidence is
    # not a credible explanation. This exists because the broad rule is unreachable on
    # an episode where most tags name words the transcript never produced: support is
    # capped by how many tags could vote at all, not by how right the answer is.
    tight = (n >= TIGHT_MIN_SUPPORT and spread <= TIGHT_SPREAD
             and baseline >= TIGHT_MIN_BASELINE)

    return OffsetEstimate(offset=mean, support=n, considered=considered,
                          spread=spread, confident=broad or tight)


def apply_offset(times: list[float], offset: float) -> list[float]:
    return [max(0.0, t + offset) for t in times]
