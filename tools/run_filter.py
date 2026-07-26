"""
End-to-end driver: tag-set JSON + video -> verified mute list -> EDL / filtered audio.

Pipeline:
  1. parse the tag-set, dedupe by ref_id, keep only allowed word categories
  2. pre-flight the runtime against the file (wrong-cut check)
  3. locate each incident's exact boundaries, then tighten until verified
  4. full-episode discovery scan for target words VidAngel never tagged
  5. cross-reference: which scan hits are already covered, which are missed
  6. emit EDL + a report

Step 4/5 output is advisory. Whisper misrecognises and hallucinates, so auto-muting
scan hits would cause over-muting; a human decides what to add.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from align import _tool, get_model, probe_duration, probe_fps
from locate import _variants, locate, snap_to_frames
from scan import cross_reference, scan
from verify import tighten
from vidangel import DEFAULT_CATEGORIES, parse

#: How far from a bucket a scan hit may sit and still be treated as that incident.
#: Measured drift maxes out near +3s; allow headroom without reaching the next bucket.
RECOVER_WINDOW = 8.0


def _variants_for(word: str) -> set[str]:
    return _variants(word)


def timecode(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def build(video: str, tagset_path: str, categories, model_size="small.en",
          do_scan=True, only_enabled=False):
    ts = parse(open(tagset_path, encoding="utf-8").read())
    fps = probe_fps(video)
    duration = probe_duration(video)

    warn = ts.check_runtime(duration)
    print(f"tag-set {ts.tag_set_id} | work {ts.work_id} | fps {fps:.3f}")
    print(f"runtime: tag-set {ts.runtime_unaltered:.0f}s vs file {duration:.1f}s -> "
          f"{warn or 'same cut'}")

    pool = ts.enabled() if only_enabled else ts.incidents
    todo = [i for i in pool if i.category_key in categories]
    print(f"\n{len(todo)} incidents in categories {tuple(categories)}\n")

    results, mutes = [], []
    model = get_model(model_size)

    for inc in todo:
        w0, w1 = inc.search_window()
        pad = (w1 - w0) / 2
        best = None
        for cand in inc.words:
            mt = locate(video, cand, inc.start_approx, inc.start_approx, fps,
                        model=model, search_pad=pad)
            if mt and (best is None or mt.confidence > best.confidence):
                best = mt

        if best is None:
            results.append({"ref_id": inc.ref_id, "word": inc.words[0],
                            "bucket": inc.start_approx, "status": "NOT_FOUND"})
            print(f"  {inc.ref_id} {inc.words[0]:<6} bucket {inc.start_approx:6.0f}  NOT FOUND")
            continue

        s, e, v, rounds = tighten(video, best.expected, best.start, best.end, fps,
                                  model=model)
        mutes.append((inc.ref_id, s, e))
        results.append({
            "ref_id": inc.ref_id, "word": best.expected, "bucket": inc.start_approx,
            "start": round(s, 3), "end": round(e, 3),
            "drift": round(s - inc.start_approx, 3),
            "confidence": round(best.confidence, 3),
            "rounds": rounds, "status": "OK" if v.ok else "REVIEW",
            "note": v.note,
        })
        print(f"  {inc.ref_id} {best.expected:<6} bucket {inc.start_approx:6.0f}  "
              f"{timecode(s)}-{timecode(e)} drift {s-inc.start_approx:+.2f} "
              f"{'OK' if v.ok else 'REVIEW'} {v.note}")

    scan_report = {}
    if do_scan:
        words = sorted({w for i in todo for w in i.words})
        print(f"\nfull-episode discovery scan for {words} ...")
        hits = scan(video, words, model=model, progress=False)

        # Recover NOT_FOUND incidents. Whisper's chunking differs between the narrow
        # per-incident window and the full scan, and decode variance means the scan
        # sometimes hears a word the targeted pass missed. If an uncovered hit sits
        # near an unresolved bucket, it is almost certainly that incident.
        unresolved = [r for r in results if r["status"] == "NOT_FOUND"]
        for r in unresolved:
            near = [
                h for h in hits
                if h.covered_by is None
                and h.word in _variants_for(r["word"])
                and abs(h.start - r["bucket"]) <= RECOVER_WINDOW
            ]
            if not near:
                continue
            h = min(near, key=lambda x: abs(x.start - r["bucket"]))
            s, e = snap_to_frames(max(0.0, h.start - 0.06), h.end + 0.06, fps)
            s, e, v, rounds = tighten(video, r["word"], s, e, fps, model=model)
            mutes.append((r["ref_id"], s, e))
            r.update(start=round(s, 3), end=round(e, 3),
                     drift=round(s - r["bucket"], 3),
                     confidence=round(h.confidence, 3), rounds=rounds,
                     status="OK_VIA_SCAN" if v.ok else "REVIEW", note=v.note)
            print(f"  recovered {r['ref_id']} {r['word']:<6} via scan at "
                  f"{timecode(s)} (drift {s - r['bucket']:+.2f}) "
                  f"{'OK' if v.ok else 'REVIEW'} {v.note}")

        covered, missed = cross_reference(hits, mutes)
        scan_report = {
            "total_hits": len(hits),
            "covered": [{"word": h.word, "at": round(h.start, 3),
                         "by": h.covered_by} for h in covered],
            "missed": [{"word": h.word, "at": round(h.start, 3),
                        "confidence": round(h.confidence, 2),
                        "context": h.context} for h in missed],
        }
        print(f"  {len(hits)} hits: {len(covered)} covered by planned mutes, "
              f"{len(missed)} NOT covered")
        for h in missed:
            print(f"    MISSED {h.timecode} {h.word:<6} p={h.confidence:.2f}  ...{h.context[:60]}...")

    # Scan-recovered mutes are appended out of order; players and humans both expect
    # a chronological list.
    mutes.sort(key=lambda m: m[1])
    return {"tag_set_id": ts.tag_set_id, "fps": fps, "duration": duration,
            "incidents": results, "mutes": mutes, "scan": scan_report}


def write_edl(report, path: str) -> None:
    """Plex/MPlayer EDL. Action 1 = mute, which is what word-level filtering wants."""
    with open(path, "w", encoding="utf-8") as fh:
        for _ref, s, e in report["mutes"]:
            fh.write(f"{s:.3f} {e:.3f} 1\n")


def render(video: str, report, path: str, archive: str | None = None,
           quality: str = "same") -> None:
    """Archive the original, then delegate rendering to the shared `render` module.

    This used to carry its own copy of the render logic, which meant fixes landing in one
    path and not the other — the multi-track and per-stream-codec bugs were fixed in
    `render.py` while this still mapped only `a:0`. One implementation now serves both the
    CLI and the web app.
    """
    mutes = report["mutes"]
    if not mutes and not report.get("video_ranges"):
        raise SystemExit("no mutes to apply")

    if archive:
        os.makedirs(os.path.dirname(archive) or ".", exist_ok=True)
        if os.path.exists(archive):
            raise SystemExit(f"archive already exists, refusing to overwrite: {archive}")
        import shutil

        shutil.copy2(video, archive)
        print(f"archived original -> {archive}")

    import render as render_mod

    stats = render_mod.render(video, path, mutes,
                              report.get("video_ranges") or [], quality=quality)
    print(f"  audio: {stats.get('summary', '')}")
    report["render"] = stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("tagset")
    ap.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES))
    ap.add_argument("--model", default="small.en")
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--only-enabled", action="store_true",
                    help="only incidents in the tag-set's enabled_tags")
    ap.add_argument("--edl")
    ap.add_argument("--out", help="render a filtered copy (lossless FLAC audio)")
    ap.add_argument("--archive", help="copy the untouched original here before rendering")
    ap.add_argument("--quality", choices=("splice", "same", "lossless"),
                    default="splice",
                    help="splice: re-encode ONLY the muted frames, byte-identical "
                         "elsewhere, size-neutral (best; falls back to lossless for "
                         "DTS-HD MA/TrueHD). same: re-encode whole track to the source "
                         "codec (~166 dB PSNR, inaudible). lossless: FLAC (bit-exact, "
                         "~1.84x audio size)")
    ap.add_argument("--json")
    args = ap.parse_args()

    cats = tuple(c.strip() for c in args.categories.split(",") if c.strip())
    report = build(args.video, args.tagset, cats, args.model,
                   do_scan=not args.no_scan, only_enabled=args.only_enabled)

    if args.edl:
        write_edl(report, args.edl)
        print(f"\nwrote EDL: {args.edl}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote report: {args.json}")
    if args.out:
        render(args.video, report, args.out, archive=args.archive, quality=args.quality)
        print(f"wrote filtered copy: {args.out}")

    ok = sum(1 for r in report["incidents"] if r["status"] == "OK")
    rev = sum(1 for r in report["incidents"] if r["status"] == "REVIEW")
    nf = sum(1 for r in report["incidents"] if r["status"] == "NOT_FOUND")
    print(f"\n{ok} verified | {rev} need review | {nf} not found")


if __name__ == "__main__":
    main()
