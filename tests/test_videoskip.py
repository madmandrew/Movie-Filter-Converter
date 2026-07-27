"""VideoSkip / EDL / JSON filter parsing tests.

The `.vsk` sample below is the exact shape the legacy 2022 app emitted (see CLAUDE.md),
so it is real ground truth rather than an invented fixture.

Run: python tests/test_videoskip.py
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"),
)

from videoskip_client import parse_any, parse_edl, parse_vsk  # noqa: E402

VSK = """00:05:11.269 --> 00:05:11.520
Profane Word 1 (hell)

00:09:38.536 --> 00:09:38.953
Profane Word 1 (ass)

00:12:00.000 --> 00:12:18.500
Sex 1 (couple in bed)

00:20:04.000 --> 00:20:09.000
Violence 1
"""

# Comma decimals, no blank lines, no category line — all seen in the wild.
VSK_MESSY = """Title: Some Movie
00:01:02,500 --> 00:01:03,000
Profane Word 1 (damn)
00:02:00,000 --> 00:02:04,000
"""

EDL = """311.269 311.520 1
578.536 578.953 1
720.000 738.500 0
bad line here
1200.0 1195.0 1
"""

JSON = """{"title":"X","entries":[
  {"start":10.5,"end":11.25,"category":"Profane Word","description":"hell"},
  {"start":60,"end":78,"category":"Nudity"},
  {"start":99,"end":99,"category":"Other"}
]}"""


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond and detail else ""))
    return bool(cond)


def main() -> int:
    ok = True
    print("=== .vsk ===")
    f = parse_vsk(VSK)
    ok &= check("4 entries parsed", len(f.entries) == 4, f"got {len(f.entries)}")
    e = f.entries[0]
    ok &= check("start parsed to ms", abs(e.start - 311.269) < 1e-6, f"{e.start}")
    ok &= check("end parsed to ms", abs(e.end - 311.520) < 1e-6, f"{e.end}")
    ok &= check("category captured", e.category == "Profane Word", e.category)
    ok &= check("description captured", e.description == "hell", e.description)
    ok &= check("Profane Word -> audio", e.kind == "audio", e.kind)
    ok &= check("Sex -> video", f.entries[2].kind == "video", f.entries[2].kind)
    ok &= check("Violence without desc parses", f.entries[3].category == "Violence")
    ok &= check("2 audio / 2 video split",
                len(f.audio()) == 2 and len(f.video()) == 2,
                f"{len(f.audio())}/{len(f.video())}")

    print("=== messy .vsk (comma decimals, no blank lines, title line) ===")
    m = parse_vsk(VSK_MESSY)
    ok &= check("title extracted", m.title == "Some Movie", m.title)
    ok &= check("2 entries", len(m.entries) == 2, f"got {len(m.entries)}")
    ok &= check("comma decimal parsed", abs(m.entries[0].start - 62.5) < 1e-6,
                str(m.entries[0].start))
    ok &= check("missing category defaults to Other",
                m.entries[1].category == "Other", m.entries[1].category)

    print("=== EDL ===")
    d = parse_edl(EDL)
    ok &= check("3 valid entries (junk + reversed dropped)",
                len(d.entries) == 3, f"got {len(d.entries)}")
    ok &= check("action 1 -> audio mute", d.entries[0].kind == "audio")
    ok &= check("action 0 -> video cut", d.entries[2].kind == "video")

    print("=== JSON ===")
    j = parse_any(JSON)
    ok &= check("zero-length entry dropped", len(j.entries) == 2, f"got {len(j.entries)}")
    ok &= check("Nudity -> video", j.entries[1].kind == "video")

    print("=== format auto-detection ===")
    ok &= check("vsk detected", len(parse_any(VSK).entries) == 4)
    ok &= check("edl detected", len(parse_any(EDL).entries) == 3)
    try:
        parse_any("this is not a filter file at all")
        ok &= check("garbage rejected", False, "no exception raised")
    except ValueError:
        ok &= check("garbage rejected", True)

    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
