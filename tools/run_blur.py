"""
Driver: detect nudity in a video and write a copy with those regions obscured.

This is the *blur* counterpart to `run_filter.py`'s audio muting. It keeps the scene and
destroys the region, which is the right trade when cutting would gut the narrative.

The pipeline verifies its own output: after rendering it re-scans the result, folds any
uncovered detection back into the plan, and re-renders. Expect the reported leak count to
fall but not necessarily reach zero — see `--help` on `--repair`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blur import blur_video
from nudity import BENIGN_CLASSES, DEFAULT_CLASSES


def timecode(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:06.3f}"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Blur or pixelate detected nudity in a video.",
        epilog="Benign classes never acted on: " + ", ".join(BENIGN_CLASSES),
    )
    ap.add_argument("video")
    ap.add_argument("out")
    ap.add_argument("--classes", default=",".join(DEFAULT_CLASSES),
                    help="comma-separated NudeNet classes to obscure")
    ap.add_argument("--min-score", type=float, default=0.25,
                    help="detection threshold (default 0.25; lower catches more marginal "
                         "regions at the cost of false positives)")
    ap.add_argument("--strength", type=float, default=0.25,
                    help="pixelate: fraction of region per block. gaussian: kernel as a "
                         "fraction of the region's smaller side. Higher = more destroyed")
    ap.add_argument("--gaussian", action="store_true",
                    help="use a Gaussian blur instead of the default mosaic")
    ap.add_argument("--dilate", type=float, default=1.9,
                    help="grow each detection box by this factor before obscuring")
    ap.add_argument("--hold", type=int, default=10,
                    help="frames to keep a box after its last detection (covers dropouts)")
    ap.add_argument("--lead", type=int, default=8,
                    help="frames to obscure before a run's first detection")
    ap.add_argument("--passes", type=int, default=2,
                    help="detection passes to union; the model is not stable at the "
                         "margins, so >1 recovers boxes a single pass misses")
    ap.add_argument("--repair", type=int, default=2,
                    help="max re-render rounds folding output leaks back into the plan. "
                         "Some detections always survive because the model reads body "
                         "shape through the mosaic; those are not leaks and cannot be "
                         "fixed by blurring harder")
    ap.add_argument("--detect-width", type=int, default=640)
    ap.add_argument("--json", help="write the summary report here")
    args = ap.parse_args()

    classes = tuple(c.strip() for c in args.classes.split(",") if c.strip())
    print(f"source: {args.video}")
    print(f"classes: {', '.join(classes)}  min_score={args.min_score}")

    report = blur_video(
        args.video, args.out,
        classes=classes, min_score=args.min_score,
        hold=args.hold, lead=args.lead, dilate=args.dilate,
        strength=args.strength, pixelate=not args.gaussian,
        detect_width=args.detect_width,
        passes=args.passes, repair=args.repair,
        progress=lambda i: print(f"  frame {i}", end="\r", flush=True),
    )

    print(" " * 40, end="\r")
    w, h = report["size"]
    print(f"\n{w}x{h} @ {report['fps']:.3f} fps, {report['total_frames']} frames")
    print(f"detected on {report['frames_detected']} frames, "
          f"obscured {report['frames_blurred']}")
    for cls, n in sorted(report["per_class"].items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d}  {cls}")

    if report["leaks_repaired"]:
        rounds = " -> ".join(str(n) for n in report["leaks_repaired"])
        print(f"leaks folded back per repair round: {rounds}")

    print("\nobscured ranges:")
    for s, e in report["ranges"]:
        print(f"  {timecode(s)} - {timecode(e)}  ({e - s:.2f}s)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nwrote report: {args.json}")
    print(f"wrote: {report['output']}")


if __name__ == "__main__":
    main()
