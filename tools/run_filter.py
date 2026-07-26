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
import tempfile
from dataclasses import dataclass

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


#: Source profiles ffmpeg CANNOT re-encode without destroying what makes them special.
#: These are lossless / object-based formats; the available encoders only produce the
#: lossy core (DTS-HD MA -> 1536k DTS core) or cannot carry object metadata at all
#: (Atmos, DTS:X). Re-encoding "to the same codec" silently downgrades them.
_UNENCODABLE = ("dts-hd", "dts:x", "truehd", "atmos", "mlp")


@dataclass
class AudioPlan:
    args: list[str]
    label: str
    warning: str | None = None


def probe_audio(video: str) -> tuple[str, str, str | None, int]:
    """(codec_name, profile, bit_rate or None, channels) of the first audio stream."""
    out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,profile,bit_rate,channels",
         "-of", "default=noprint_wrappers=1:nokey=0", "--", video],
        capture_output=True, text=True, check=True,
    ).stdout
    vals = dict(
        line.split("=", 1) for line in out.strip().splitlines() if "=" in line
    )
    br = vals.get("bit_rate", "N/A")
    return (
        vals.get("codec_name", "unknown"),
        vals.get("profile", "unknown"),
        br if br.isdigit() else None,
        int(vals.get("channels") or 0),
    )


def audio_args(video: str, mode: str) -> AudioPlan:
    """Choose ffmpeg audio arguments for the requested quality mode.

    Muting inherently requires decode -> zero the span -> re-encode; `-c:a copy` cannot
    apply a filter, so "leave the original stream untouched" is not achievable.

    Modes:

    * ``same`` - re-encode to the source codec at its original bitrate. Keeps file size
      flat. Measured on plain DTS 5.1 1536 kbps: **161-172 dB PSNR** across all six
      channels, which is below the noise floor of 24-bit audio (~144 dB) and therefore
      inaudible. Good for TV, where disk space matters more than the last dB.

    * ``lossless`` - FLAC. Measured **inf dB PSNR** — bit-exact to the decoded source.
      ~1.84x the audio size. The right choice when the audio experience matters.

    **Critical caveat:** lossless and object-based sources (DTS-HD MA, DTS:X, TrueHD,
    Atmos) have **no usable ffmpeg encoder**. `same` would quietly downgrade DTS-HD MA
    to its lossy 1536k core and discard Atmos/DTS:X object metadata entirely. For those
    sources `same` is refused and FLAC is used instead — FLAC preserves every decoded
    sample, though the object metadata is lost either way (an unavoidable consequence of
    filtering such a track at all).
    """
    codec, profile, bitrate, channels = probe_audio(video)
    prof_l = (profile or "").lower()
    lossless_src = any(k in prof_l for k in _UNENCODABLE) or codec in ("truehd", "mlp")

    if mode == "lossless":
        return AudioPlan(
            ["-c:a", "flac", "-compression_level", "8"],
            f"FLAC lossless (from {codec}/{profile}, {channels}ch)",
        )

    if lossless_src:
        objects = "atmos" in prof_l or "dts:x" in prof_l
        if codec in ("truehd", "mlp"):
            why = ("ffmpeg's truehd encoder is experimental and cannot carry Atmos "
                   "object metadata")
        else:
            why = (f"ffmpeg cannot encode {profile}; the 'dca' encoder only produces "
                   f"the lossy DTS core, discarding the lossless extension")
        return AudioPlan(
            ["-c:a", "flac", "-compression_level", "8"],
            f"FLAC lossless (forced; {profile} has no usable encoder)",
            warning=(
                f"source is {profile} ({channels}ch) — {why}. Using FLAC instead, which "
                f"preserves every decoded sample bit-exactly."
                + (" Object metadata (Atmos/DTS:X) is lost by any filtering of this "
                   "track — it cannot survive a decode/re-encode cycle." if objects else "")
            ),
        )

    encoder = {"dts": "dca"}.get(codec, codec)
    args = ["-strict", "-2", "-c:a", encoder]
    if bitrate:
        args += ["-b:a", bitrate]
    return AudioPlan(
        args,
        f"{codec} @ {int(bitrate)//1000 if bitrate else '?'}k ({channels}ch, same as source)",
    )


def render(video: str, report, path: str, archive: str | None = None,
           quality: str = "same") -> None:
    """Write a filtered copy, and archive the untouched original.

    Video is always stream copied, so the picture is bit-identical. The result carries a
    single audio track: the filtered one. Keeping the original as a second track in the
    same file was considered and rejected — one file, one track, no chance of picking
    the wrong one by accident.

    `archive` copies the original aside first. This is mandatory in the real workflow
    (video cuts are destructive, so the original is the only way to re-run from the raw
    cut), and is why the filtered file may safely replace the library copy.
    """
    mutes = report["mutes"]
    if not mutes:
        raise SystemExit("no mutes to apply")

    if archive:
        os.makedirs(os.path.dirname(archive) or ".", exist_ok=True)
        if os.path.exists(archive):
            raise SystemExit(f"archive already exists, refusing to overwrite: {archive}")
        import shutil

        shutil.copy2(video, archive)
        print(f"archived original -> {archive}")

    spans = [(s, e) for _r, s, e in mutes]

    if quality == "splice":
        import splice as splice_mod

        info = splice_mod.probe(video)
        ok, reason = splice_mod.can_splice(info)
        if not ok:
            print(f"  splice unavailable ({reason})")
            print("  falling back to --quality lossless (FLAC)")
            quality = "lossless"
        else:
            print(f"  audio: splice — {reason}")
            tmpdir = tempfile.mkdtemp(prefix="splice_out_")
            muted_audio = os.path.join(tmpdir, f"audio.{info.codec}")
            stats = splice_mod.splice_audio(video, spans, muted_audio, info=info)
            pct = 100.0 * stats["bytes_reencoded"] / max(
                1, stats["bytes_reencoded"] + stats["bytes_copied"])
            print(f"         {stats['muted_segments']} mutes, "
                  f"{stats['bytes_reencoded']:,} of "
                  f"{stats['bytes_reencoded'] + stats['bytes_copied']:,} bytes "
                  f"re-encoded ({pct:.2f}%) — the rest is byte-identical")
            # Remux: original video/subs stream copied, audio replaced wholesale.
            subprocess.run(
                [_tool("ffmpeg"), "-v", "error", "-y",
                 "-i", video, "-i", muted_audio,
                 "-map", "0:v", "-map", "1:a", "-map", "0:s?",
                 "-c", "copy", "-shortest", path],
                check=True,
            )
            return

    plan = audio_args(video, quality)
    if plan.warning:
        print(f"  WARNING: {plan.warning}")
    print(f"  audio: {plan.label}")

    expr = "+".join(f"between(t,{s:.4f},{e:.4f})" for s, e in spans)
    subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-y", "-i", video,
         "-af", f"volume=0:enable='{expr}'",
         "-c:v", "copy",
         *plan.args,
         # Carry subtitles and chapters through untouched.
         "-c:s", "copy", "-map", "0",
         path],
        check=True,
    )


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
