"""
Parse a release filename into a searchable title, year, and season/episode.

Auto-fetching filters means matching a file like
`The.Lone.Ranger.2013.1080p.BluRay.DTS.x264-PHD.mkv` to a VidAngel title. The scene
naming convention is loose but consistent enough to strip reliably: everything from the
first quality/source/codec token onward is metadata, and the year (or SxxEyy) marks the
end of the title.

Written against real filenames from the user's library rather than invented examples, and
covered by tests that use those same names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Tokens that mark the end of the title and the start of release metadata. Ordered
#: longest-first at match time so "DTS-HD" wins over "DTS".
_STOP_TOKENS = {
    # resolution / source
    "2160p", "1080p", "720p", "480p", "4k", "uhd", "hd", "sd",
    "bluray", "blu-ray", "brrip", "bdrip", "bdremux", "remux", "webrip", "web-dl",
    "webdl", "web", "hdtv", "hdtvrip", "dvdrip", "dvd", "hdrip", "camrip", "ts",
    # codecs
    "x264", "x265", "h264", "h265", "h", "hevc", "avc", "xvid", "divx", "10bit",
    "8bit", "hdr", "hdr10", "dv", "sdr",
    # audio
    "dts", "dts-hd", "dtshd", "truehd", "atmos", "ac3", "eac3", "aac", "ddp5",
    "dd5", "dd2", "ma", "flac", "mp3", "5", "7", "multisubs", "multi", "subs",
    # misc release markers
    "proper", "repack", "internal", "limited", "unrated", "extended", "directors",
    "director", "theatrical", "remastered", "anniversary", "special", "edition",
    "collectors", "cut", "complete", "season", "converted",
}

#: Recognise a 4-digit year, but not a number that is part of the title (e.g. "1883",
#: "2012" the film). Only treated as a year when it sits after at least one word.
_YEAR_RE = re.compile(r"^(19\d{2}|20\d{2})$")
#: Ordinal-anniversary markers scene releases insert mid-title
#: (`Memento.10th.Anniversary.Special.Edition…`). They precede "Anniversary", which is
#: already a stop token, but the ordinal itself would otherwise survive into the title.
_ORDINAL_RE = re.compile(r"^\d{1,3}(st|nd|rd|th)$", re.I)
_SXXEYY_RE = re.compile(r"^s(\d{1,2})[\s._-]*e(\d{1,3})$", re.I)
#: A bare season marker, as used by season packs (`Community.S01.1080p…`). It ends the
#: title just like SxxEyy does, but names no episode.
_SEASON_ONLY_RE = re.compile(r"^s(\d{1,2})$", re.I)
_SXXEYY_INLINE = re.compile(r"\bs(\d{1,2})[\s._-]*e(\d{1,3})\b", re.I)
_SEASON_WORD = re.compile(r"\b(?:season|series)[\s._-]*(\d{1,2})\b", re.I)
_EPISODE_WORD = re.compile(r"\b(?:episode|ep)[\s._-]*(\d{1,3})\b", re.I)


@dataclass
class ParsedTitle:
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None

    @property
    def is_episode(self) -> bool:
        return self.season is not None and self.episode is not None

    @property
    def query(self) -> str:
        """What to send to a title search."""
        return self.title

    def __str__(self) -> str:
        bits = [self.title]
        if self.year:
            bits.append(f"({self.year})")
        if self.is_episode:
            bits.append(f"S{self.season:02d}E{self.episode:02d}")
        return " ".join(bits)


def _normalise(stem: str) -> list[str]:
    """Split a filename stem into words, treating . _ - and space as separators.

    Hyphens are ambiguous — they separate the release group (`-PHD`) but also appear
    inside titles (`Spider-Man`) and codec names (`DTS-HD`). Splitting on them and relying
    on the stop-token scan to end the title first handles all three.
    """
    stem = re.sub(r"\[[^\]]*\]", " ", stem)          # [rartv], [YTS] etc.
    # Parenthesised groups are usually release notes, EXCEPT a bare year — the
    # `Title (2008).mp4` convention is common and the year is the strongest
    # disambiguator, so keep it and drop the rest.
    stem = re.sub(r"\((\s*(?:19|20)\d{2}\s*)\)", r" \1 ", stem)
    stem = re.sub(r"\([^)]*\)", " ", stem)
    stem = stem.replace("'", "")                      # don't split possessives
    return [w for w in re.split(r"[\s._\-+]+", stem) if w]


def parse(filename: str) -> ParsedTitle:
    """Extract title/year/season/episode from a release filename."""
    stem = re.sub(r"\.(mkv|mp4|m4v|avi|mov|ts|m2ts)$", "", filename, flags=re.I)

    season = episode = None
    m = _SXXEYY_INLINE.search(stem)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
    else:
        sm, em = _SEASON_WORD.search(stem), _EPISODE_WORD.search(stem)
        if sm and em:
            season, episode = int(sm.group(1)), int(em.group(1))

    words = _normalise(stem)
    title_words: list[str] = []
    year: int | None = None

    # Find the year independently of the title scan. Edition markers can sit between the
    # title and the year ("Memento 10th Anniversary Special Edition Remastered 2000"), so
    # a scan that stops at the marker would otherwise lose it — and the year is the
    # strongest signal for telling remakes and sequels apart. Take the LAST plausible
    # year before the resolution/codec tokens, since a title can itself contain one.
    for i, w in enumerate(words):
        if i and _YEAR_RE.match(w):
            year = int(w)

    for w in words:
        low = w.lower()
        # SxxEyy, or a bare season marker on a season pack, ends the title.
        if _SXXEYY_RE.match(low) or (_SEASON_ONLY_RE.match(low) and title_words):
            break
        # A year ends the title, but only once something precedes it — otherwise a title
        # that *is* a year ("1883", "2012") would parse as empty. The value itself was
        # already captured by the independent scan above.
        if _YEAR_RE.match(low) and title_words:
            break
        if low in _STOP_TOKENS and title_words:
            break
        # "10th" in "Memento 10th Anniversary Special Edition": the ordinal precedes a
        # stop token, so treat it as the start of the edition marker rather than title.
        if _ORDINAL_RE.match(low) and title_words:
            break
        title_words.append(w)

    title = " ".join(title_words).strip()
    # Trailing junk a stop-token scan can leave behind.
    title = re.sub(r"\s+(the|a|an)$", "", title, flags=re.I).strip()
    return ParsedTitle(title=title or stem, year=year, season=season, episode=episode)


#: Region and qualifier suffixes scene releases add that catalogues usually omit —
#: "The Office US" vs "The Office". Stripped only for comparison, never from the parsed
#: title, so the user still sees what the file actually says.
_REGION_SUFFIXES = ("us", "uk", "usa", "au", "nz", "ca")


def _strip_region(words: list[str]) -> list[str]:
    return words[:-1] if len(words) > 1 and words[-1] in _REGION_SUFFIXES else words


def score_match(parsed: ParsedTitle, candidate_title: str,
                candidate_year: int | None) -> int:
    """How well a search result matches a parsed filename, 0-100.

    Deliberately conservative: auto-fetching the wrong filter set is worse than fetching
    nothing, so a confident score requires the title to match closely AND the year to
    agree when both are known.
    """
    a = re.sub(r"[^a-z0-9 ]", " ", parsed.title.lower())
    b = re.sub(r"[^a-z0-9 ]", " ", (candidate_title or "").lower())
    a = re.sub(r"\s+", " ", a).strip()
    b = re.sub(r"\s+", " ", b).strip()
    if not a or not b:
        return 0

    if a == b:
        score = 100
    elif a.replace(" ", "") == b.replace(" ", ""):
        score = 95
    elif " ".join(_strip_region(a.split())) == " ".join(_strip_region(b.split())):
        # "The Office US" vs "The Office" — the file names a region the catalogue omits.
        score = 92
    else:
        aw, bw = set(a.split()), set(b.split())
        if not aw or not bw:
            return 0
        overlap = len(aw & bw) / max(len(aw), len(bw))
        if aw <= bw or bw <= aw:
            score = 80          # one is a subset: "Rush Hour" vs "Rush Hour 2"
        elif overlap >= 0.8:
            score = 70
        elif overlap >= 0.5:
            score = 45
        else:
            score = int(overlap * 40)

    # Year agreement is the strongest disambiguator between remakes and sequels.
    if parsed.year and candidate_year:
        delta = abs(parsed.year - candidate_year)
        if delta == 0:
            score = min(100, score + 10)
        elif delta == 1:
            score -= 5          # release-year vs production-year disagreement is common
        else:
            score -= 40         # almost certainly a different film
    return max(0, min(100, score))


#: Minimum score to auto-fetch without asking. Chosen so that an exact title with a
#: matching year (110 -> capped 100) or an exact title alone (100) qualifies, while a
#: subset match like "Rush Hour" vs "Rush Hour 2" (80) does not.
AUTO_THRESHOLD = 90
