"""
Parse VidAngel's `/api/bff/tag-sets/<id>/` payload into flat, locatable incidents.

Key facts about this format, learned from real data (tag-set 46025):

* `start_approx` values are all multiples of **6** — the timings are 6-second
  buckets, not seconds. Search windows must be at least +/-6s.
* Audio tags are points: `start_approx == end_approx`. `end_approx` carries no
  duration information for them.
* `description` is censored (`"h*ll"`, `"*ss"`) but the category `key` holds the word
  in the clear (`"hell"`, `"ass"`). The key is the match target.
* Tags whose key starts with `other_` have a prose description and no word at all
  ("A man references male genitalia with a slang term."). These cannot be
  word-spotted and are flagged for manual handling.
* The same tag is repeated under multiple categories via `is_symlink`, so dedupe by
  `ref_id`.
* `runtime_unaltered` vs. the local file's duration is a free pre-flight check for
  the wrong-master problem.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: Category keys that describe a *kind of content* rather than a spoken word. Searching
#: the audio for "immodesty" or "violence" would find nothing, and worse, a match on the
#: literal word would mute an innocent mention of it. Observed on real tag-sets.
_NON_WORD_KEYS = frozenset({
    "profanity", "blasphemy", "language", "language_racial", "language_childish",
    "language_sexual", "immodesty", "immodesty_male", "immodesty_female",
    "immodesty_both", "nudity", "nudity_male", "nudity_female", "sex_any",
    "non_graphic", "graphic", "violence", "gore", "objectionable", "implied",
    "implied_not_shown", "shown_w_nudity", "shown_w_o_nudity", "sexual_assault",
    "sexually_suggestive", "vulgar_gestures", "bodily_functions", "human_functions",
    "life_events", "credits", "opening_credits", "closing_credits",
    "alcohol_or_drug_use", "drugs_legal", "drugs_implied", "drugs_illegal",
    "smoking", "gambling", "occult", "disturbing", "intense", "scary",
})

#: Categories to act on. Deliberately narrow: the goal is muting individual swear
#: words, NOT blanking whole phrases, sentences, or scenes. Prose-described tags
#: ("A man makes a sexual reference about women.") name no word, so word-spotting
#: cannot place them and a mute would have to swallow the whole utterance — out of
#: scope by choice, not by limitation.
DEFAULT_CATEGORIES = ("damn", "hell", "ass")

#: Category keys that name a specific word. Maps the API key to what is actually
#: said on screen, where they differ.
_WORD_KEYS = {
    "ass": ["ass", "asshole"],
    "damn": ["damn", "goddamn", "dammit"],
    "hell": ["hell"],
    "god": ["god", "goddamn", "jesus", "christ"],
    "douche": ["douche", "douchebag"],
    "shit": ["shit", "bullshit"],
    "bitch": ["bitch"],
    "bastard": ["bastard"],
    "crap": ["crap"],
    "piss": ["piss", "pissed"],
    "dick": ["dick"],
    "fuck": ["fuck", "fucking", "fucker"],
    # Keys seen on real tag-sets that were missing here. Their absence made the incident
    # unlocatable, and since a category name is the only fallback, selecting one by hand
    # crashed on an empty word list.
    "jesus": ["jesus", "christ"],
    "christ": ["christ", "jesus"],
    "cock": ["cock"],
    "pussy": ["pussy"],
    "whore": ["whore"],
    "slut": ["slut"],
    "fag": ["fag", "faggot"],
    "nigger": ["nigger", "nigga"],
    "retard": ["retard", "retarded"],
    "bollocks": ["bollocks"],
    "bugger": ["bugger"],
    "wanker": ["wanker"],
    "twat": ["twat"],
    "prick": ["prick"],
    "cunt": ["cunt"],
    "arse": ["arse", "arsehole"],
    "bloody": ["bloody"],
    "stupid": ["stupid"],
    "idiot": ["idiot"],
    "moron": ["moron"],
    "jackass": ["jackass"],
    "screw": ["screw", "screwed"],
    "suck": ["suck", "sucks"],
    "freaking": ["freaking", "fricking"],
}

#: Tag times are quantised to 6s multiples, but measurement on real data shows the
#: word is NOT confined to [start_approx, start_approx + 6): observed drifts run from
#: -0.7s to +3.0s relative to the bucket. Treat the bucket as a centre-ish hint and
#: search generously either side — a wide window costs ~1s of GPU time, a missed word
#: costs a failed filter.
BUCKET_SECONDS = 6
SEARCH_PAD = 7.0


@dataclass
class Incident:
    ref_id: str
    tag_id: int
    category_key: str
    category_title: str
    description: str
    kind: str                      # "audio" | "audiovisual"
    start_approx: float
    end_approx: float
    enabled: bool
    parents: list[str] = field(default_factory=list)

    @property
    def words(self) -> list[str]:
        """Candidate spoken words, or [] if this incident has no specific word.

        Falls back to the category key itself when it is not in `_WORD_KEYS`. VidAngel
        names word categories after the word (`cock`, `jesus`, `bollocks`), so the key is
        a good guess and keeps a newly-seen category locatable instead of silently
        unusable — the map can never be exhaustive. `other_*` keys are prose descriptions
        of an action, not words, so they stay empty.
        """
        mapped = _WORD_KEYS.get(self.category_key)
        if mapped:
            return mapped
        key = self.category_key
        if (key and not key.startswith("other")
                and key not in _NON_WORD_KEYS and key.isalpha()):
            return [key]
        return []

    @property
    def locatable(self) -> bool:
        """True if word-spotting can find this incident.

        `other_*` categories carry only a prose description of the action, and
        audiovisual tags may have no speech at all — neither is word-spottable.
        """
        return bool(self.words) and self.kind == "audio"

    @property
    def is_structural(self) -> bool:
        """Opening/closing credits — real ranges, but not content to mute."""
        return self.category_key in ("opening_credits", "closing_credits")

    def search_window(self, pad: float = SEARCH_PAD) -> tuple[float, float]:
        """Window that should contain the incident.

        Observed drift on real data spans -0.7s to +3.0s from `start_approx`, so pad
        both directions rather than assuming the word sits inside the nominal bucket.
        `end_approx` only adds information when it differs from `start_approx`.
        """
        lo = self.start_approx - pad
        hi = max(self.end_approx, self.start_approx + BUCKET_SECONDS) + pad
        return max(0.0, lo), hi


@dataclass
class TagSet:
    tag_set_id: int
    work_id: int
    runtime_unaltered: float
    incidents: list[Incident]

    def enabled(self) -> list[Incident]:
        return [i for i in self.incidents if i.enabled]

    def check_runtime(self, actual_duration: float, tolerance: float = 10.0) -> str | None:
        """Warn if the tag-set looks keyed to a different cut than the local file.

        A large gap means every timing is offset (the historical +12s cases), and
        no amount of precision will fix it without a global alignment first.
        """
        delta = actual_duration - self.runtime_unaltered
        if abs(delta) > tolerance:
            return (
                f"runtime mismatch: tag-set says {self.runtime_unaltered:.0f}s, file is "
                f"{actual_duration:.0f}s (delta {delta:+.0f}s) - likely a different cut"
            )
        return None


def parse(payload: dict | str) -> TagSet:
    """Flatten the nested category tree into deduped incidents."""
    if isinstance(payload, str):
        payload = json.loads(payload)

    enabled_ids = set(payload.get("enabled_tags") or [])
    by_ref: dict[str, Incident] = {}

    def walk(cat: dict, trail: list[str]) -> None:
        key = cat.get("key", "")
        title = cat.get("display_title", "")
        here = trail + [title]

        for tag in cat.get("tags") or []:
            ref = str(tag.get("ref_id"))
            if ref in by_ref:
                # Symlinked into another category; keep the first, richest trail.
                continue
            by_ref[ref] = Incident(
                ref_id=ref,
                tag_id=tag["id"],
                category_key=key,
                category_title=title,
                description=tag.get("description", ""),
                kind=tag.get("type", "audio"),
                start_approx=float(tag["start_approx"]),
                end_approx=float(tag["end_approx"]),
                enabled=ref in enabled_ids,
                parents=here[:-1],
            )

        for child in cat.get("child_categories") or []:
            walk(child, here)

    for top in payload.get("tag_categories") or []:
        walk(top, [])

    incidents = sorted(by_ref.values(), key=lambda i: i.start_approx)
    return TagSet(
        tag_set_id=payload.get("tag_set_id"),
        work_id=payload.get("work_id"),
        runtime_unaltered=float(payload.get("runtime_unaltered") or 0),
        incidents=incidents,
    )
