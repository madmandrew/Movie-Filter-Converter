"""Release-filename parsing tests.

Every case below is a real filename from the user's library, or a real VidAngel search
result. Auto-fetching depends on this: a wrong parse means fetching the wrong filter set,
which is worse than fetching nothing.

Run: python tests/test_titleparse.py
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"),
)

from titleparse import AUTO_THRESHOLD, parse, score_match  # noqa: E402

# (filename, expected title, year, season, episode)
CASES = [
    ("The.Lone.Ranger.2013.1080p.BluRay.DTS.x264-PHD.mkv",
     "The Lone Ranger", 2013, None, None),
    ("Fun with Dick and Jane.2005.1080p.HDTVRip.h264.mkv",
     "Fun with Dick and Jane", 2005, None, None),
    ("Rush Hour 1998 BluRay 1080p Dts-HD Ma7.1 Multi H264-PiR8.mkv",
     "Rush Hour", 1998, None, None),
    ("Stardust.2007.MultiSubs.BluRay.1080p.DTS-HD.MA-5.1.x264-DrSi.mkv",
     "Stardust", 2007, None, None),
    ("Deadpool.2016.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.TrueHD.7.1.Atmos-FGT-converted.mkv",
     "Deadpool", 2016, None, None),
    ("Titan A E 2000 1080p WEBRip DD5 1 x264-NTb.mkv",
     "Titan A E", 2000, None, None),
    ("John Carter 2012 1080p BRRip x264 DTS-HD MA 5.1-decatora27.mkv",
     "John Carter", 2012, None, None),
    ("The.X.Files.I.Want.to.Believe.2008.Extended.Cut.Blu-ray.1080p.AVC.mkv",
     "The X Files I Want to Believe", 2008, None, None),
    ("The.Twilight.Saga.New.Moon.2009.Extended.Cut.WebRip.H264.AC3.mp4",
     "The Twilight Saga New Moon", 2009, None, None),
    ("Oppenheimer.2023.2160p.UHD.BluRay.HDR.DTS-HD MA 5.1.x265-SPHD.mkv",
     "Oppenheimer", 2023, None, None),
    ("8.Mile.2002.2160p.UHD.Bluray.REMUX.HDR10.HEVC.DTS-X.7.1-GHD.mkv",
     "8 Mile", 2002, None, None),
    # TV
    ("Community.S01E02.1080p.BluRay.x264-YELLOWBiRD.mkv",
     "Community", None, 1, 2),
    ("The.Office.US.S07E13.1080p.BluRay.x265-RARBG.mp4",
     "The Office US", None, 7, 13),
    ("Fringe.S02E02.Night.of.Desirable.Objects.1080p.BluRay.10Bit.DDP5.1.H265-d3g.mkv",
     "Fringe", None, 2, 2),
    ("Stargate.SG-1.S08E16.Reckoning.1.1080p.BluRay.DD5.1.AVC-PiR8.mkv",
     "Stargate SG 1", None, 8, 16),
    ("Adventure.Time.With.Finn.And.Jake.S01E24.1080p.BluRay.x264-DEiMOS.mkv",
     "Adventure Time With Finn And Jake", None, 1, 24),
    ("Star.Trek.TNG.S03E15.Yesterdays.Enterprise.1080p.BluRay.10Bit.DDP5.1.H265-d3g.mkv",
     "Star Trek TNG", None, 3, 15),
    # Bracketed release tags
    ("Community.S01.1080p.BluRay.x264-YELLOWBiRD[rartv].mkv",
     "Community", None, None, None),
    # Edition markers with an ordinal, from a real library file.
    ("Memento.10th.Anniversary.Special.Edition.Remastered.2000.BluRay.1080p.mkv",
     "Memento", 2000, None, None),
    ("No.Country.For.Old.Men.2007.Collectors.Edition.BluRay.1080p.mkv",
     "No Country For Old Men", 2007, None, None),
]

# (parsed filename, candidate title, candidate year, must auto-fetch?)
MATCHES = [
    ("Oppenheimer.2023.2160p.UHD.BluRay.mkv", "Oppenheimer", 2023, True),
    ("8.Mile.2002.2160p.UHD.Bluray.mkv", "8 Mile", 2002, True),
    ("The.Lone.Ranger.2013.1080p.BluRay.mkv", "The Lone Ranger", 2013, True),
    ("Community.S01E02.1080p.BluRay.mkv", "Community", 2009, True),
    # Right title, wrong film: a remake decades apart must NOT auto-fetch.
    ("The.Lone.Ranger.2013.1080p.BluRay.mkv", "The Lone Ranger", 1956, False),
    # Sequel confusion: a subset match must NOT auto-fetch.
    ("Rush Hour 1998 BluRay 1080p.mkv", "Rush Hour 2", 2001, False),
    ("Deadpool.2016.2160p.BluRay.mkv", "Deadpool 2", 2018, False),
    # Unrelated titles that share a word.
    ("8.Mile.2002.2160p.mkv", "Super 8", 2011, False),
    ("Community.S01E02.mkv", "Together in Song: A Community in Music", 2026, False),
    # Scene releases tag a region the catalogue omits; this should still auto-fetch.
    ("The.Office.US.S07E13.1080p.BluRay.x265-RARBG.mp4", "The Office", 2005, True),
    ("Shameless.US.S01E01.1080p.mkv", "Shameless", 2011, True),
    # ...but a region suffix must not paper over a genuinely different title.
    ("The.Office.US.S07E13.1080p.mkv", "The Office Christmas Specials", 2005, False),
]


def main() -> int:
    ok = True
    print("=== filename parsing ===")
    for fn, want_title, want_year, want_s, want_e in CASES:
        p = parse(fn)
        good = (p.title == want_title and p.year == want_year
                and p.season == want_s and p.episode == want_e)
        ok &= good
        mark = "PASS" if good else "FAIL"
        print(f"  {mark}  {fn[:52]:<52} -> {p}")
        if not good:
            print(f"        expected: {want_title!r} year={want_year} "
                  f"S{want_s}E{want_e}")

    print("\n=== match scoring (auto-fetch decisions) ===")
    for fn, cand, cyear, should_auto in MATCHES:
        p = parse(fn)
        s = score_match(p, cand, cyear)
        auto = s >= AUTO_THRESHOLD
        good = auto == should_auto
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {p.title[:22]:<22} vs "
              f"{cand[:26]:<26} ({cyear}) score={s:>3} auto={auto} "
              f"{'' if good else f'(expected auto={should_auto})'}")

    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
