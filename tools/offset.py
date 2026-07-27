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
    deltas: list[float] = []
    considered = 0

    for want_t, word in expected:
        w = (word or "").lower()
        if not w:
            continue
        cands = [obs_t - want_t for obs_t, obs_w in observed
                 if obs_w.lower() == w and abs(obs_t - want_t) <= max_offset]
        if cands:
            considered += 1
            deltas.extend(cands)

    if not deltas:
        return OffsetEstimate(0.0, 0, considered, 0.0, False)

    # Densest cluster: for each delta, count neighbours within tolerance.
    deltas.sort()
    best_members: list[float] = []
    for d in deltas:
        members = [x for x in deltas if abs(x - d) <= tolerance]
        if len(members) > len(best_members):
            best_members = members

    n = len(best_members)
    mean = sum(best_members) / n
    var = sum((x - mean) ** 2 for x in best_members) / n
    spread = var ** 0.5

    # Require both enough agreement and a majority of tags that had any candidate;
    # a tight cluster of two proves nothing.
    confident = n >= min_support and n >= max(1, considered) * 0.5

    return OffsetEstimate(offset=mean, support=n, considered=considered,
                          spread=spread, confident=confident)


def apply_offset(times: list[float], offset: float) -> list[float]:
    return [max(0.0, t + offset) for t in times]
