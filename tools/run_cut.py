"""
Driver: detect nudity in a video and write a copy with those ranges removed entirely.

The cutting counterpart to `run_blur.py`. Where blurring keeps the runtime and destroys
the region, this shortens the file — so the report leads with what was removed and what
survived, which are the two things worth checking before trusting the output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cut import cut_video
from nudity import BENIGN_CLASSES, DEFAULT_CLASSES


def timecode(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:06.3f}"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Cut detected nudity ranges out of a video.",
        epilog="Benign classes never acted on: " + ", ".join(BENIGN_CLASSES),
    )
    ap.add_argument("video")
    ap.add_argument("out")
    ap.add_argument("--classes", default=",".join(DEFAULT_CLASSES),
                    help="comma-separated NudeNet classes to cut on")
    ap.add_argument("--min-score", type=float, default=0.35,
                    help="detection threshold (default 0.35)")
    ap.add_argument("--sample-fps", type=float, default=4.0,
                    help="detection sampling rate. Higher costs scan time but a missed "
                         "sample means content survives the cut")
    ap.add_argument("--min-hits", type=int, default=2,
                    help="samples needed before a range counts; 1 acts on single-frame "
                         "detections, which are usually false positives")
    ap.add_argument("--pad", type=float, default=1.0,
                    help="seconds added to each edge when no shot boundary is near")
    ap.add_argument("--tolerance", type=float, default=2.0,
                    help="how far to look for a shot boundary to snap an edge onto")
    ap.add_argument("--merge-gap", type=float, default=3.0,
                    help="merge cuts separated by less than this")
    ap.add_argument("--group-gap", type=float, default=3.0,
                    help="merge detections this far apart into one range")
    ap.add_argument("--min-keep", type=float, default=0.5,
                    help="drop kept segments shorter than this")
    ap.add_argument("--scene-threshold", type=float, default=0.35,
                    help="ffmpeg scene score for shot-boundary detection")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip re-scanning the output")
    ap.add_argument("--json", help="write the summary report here")
    args = ap.parse_args()

    classes = tuple(c.strip() for c in args.classes.split(",") if c.strip())
    print(f"source: {args.video}")
    print(f"classes: {', '.join(classes)}  min_score={args.min_score}")

    report = cut_video(
        args.video, args.out,
        classes=classes, min_score=args.min_score,
        sample_fps=args.sample_fps, min_hits=args.min_hits,
        group_gap=args.group_gap, merge_gap=args.merge_gap,
        pad=args.pad, tolerance=args.tolerance,
        scene_threshold=args.scene_threshold, min_keep=args.min_keep,
        verify=not args.no_verify,
        progress=lambda i, n: print(f"  frame {i}/{n}", end="\r", flush=True),
    )

    print(" " * 40, end="\r")
    src, out = report["source_duration"], report["output_duration"]
    print(f"\nshot boundaries found: {report['shot_cuts_found']}")
    if report["shot_cuts_found"] == 0:
        print("  (no shot changes — every edge is padded, so joins will be visible)")

    print(f"\n{len(report['cuts'])} range(s) removed, {report['removed']:.2f}s total:")
    for c in report["cuts"]:
        print(f"  {timecode(c['start'])} - {timecode(c['end'])}  "
              f"({c['duration']:.2f}s)  edges={c['edges']}  peak={c['peak_score']:.2f}")
        print(f"      {', '.join(c['classes'])}")

    print(f"\nkept {len(report['keeps'])} segment(s):")
    for s, e in report["keeps"]:
        print(f"  {timecode(s)} - {timecode(e)}  ({e - s:.2f}s)")

    pct = (out / src * 100) if src else 0
    print(f"\nruntime: {src:.2f}s -> {out:.2f}s ({pct:.1f}% kept)")

    v = report.get("verify")
    if v:
        if v["clean"]:
            print("verify: clean — nothing detected in the output")
        else:
            print(f"verify: {len(v['surviving_ranges'])} range(s) SURVIVED the cut:")
            for r in v["surviving_ranges"]:
                print(f"  {timecode(r['start'])} - {timecode(r['end'])}  "
                      f"peak={r['peak_score']:.2f}  {', '.join(r['classes'])}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nwrote report: {args.json}")
    print(f"wrote: {report['output']}")


if __name__ == "__main__":
    main()
