"""
Render a filtered file: audio mutes and/or video cuts.

The two operations are fundamentally different and must not be confused:

* **Audio mute** — silence a span. The timeline is unchanged, so mute times are absolute
  and independent. `splice.py` can do this without touching the surrounding audio.
* **Video cut** — *remove* a span. Everything after it shifts earlier, so cuts must be
  applied together in one pass, and any audio mute after a cut lands at a different
  output time than its input time.

Because of that interaction, a run with video cuts cannot reuse the byte-splice path: the
audio has to be re-assembled around the removed sections. Audio-only runs take the
splice path and stay byte-identical outside the mutes.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from align import _tool


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True)


#: Hardware encoder candidates, tried in order and **verified by actually encoding**.
#: Listing an encoder in `ffmpeg -encoders` does NOT mean it works: on this machine
#: `h264_nvenc` is listed but fails at runtime because the NVIDIA driver exposes nvenc
#: API 13.0 while the ffmpeg build requires 13.1. Only a real encode proves it.
#:
#: Measured on 60 s of 1080p (Community S01E02):
#:   h264_qsv (Intel Iris Xe)  9.1 s  = 6.62x realtime, 39.2 MB   <- best
#:   libx264 veryfast         25.6 s  = 2.35x realtime, 53.5 MB
#:   libx264 faster           43.2 s  = 1.39x realtime, 58.9 MB
#:   libx264 medium           77.5 s  = 0.77x realtime, 60.0 MB
#:   libx264 slow            127.3 s  = 0.47x realtime, 70.4 MB   <- unusable
_HW_CANDIDATES = {
    "h264": [
        (["-c:v", "h264_qsv", "-global_quality", "20"], "h264_qsv"),
        (["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20",
          "-b:v", "0"], "h264_nvenc"),
        (["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "20",
          "-qp_p", "20"], "h264_amf"),
    ],
    "hevc": [
        (["-c:v", "hevc_qsv", "-global_quality", "22"], "hevc_qsv"),
        (["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "22",
          "-b:v", "0"], "hevc_nvenc"),
    ],
}

_encoder_cache: dict[str, list[str]] = {}


def _encoder_works(args: list[str]) -> bool:
    """Try a 12-frame encode of colour bars. Cheap, and catches driver mismatches."""
    try:
        subprocess.run(
            [_tool("ffmpeg"), "-v", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=0.5",
             *args, "-f", "null", os.devnull],
            check=True, capture_output=True, timeout=60,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _video_encoder(src: str, prefer_hw: bool = True) -> list[str]:
    """Video encoder args for a re-encode, preferring verified hardware encoders.

    Video cuts force a re-encode — arbitrary, non-keyframe ranges cannot be removed by
    stream copy. Software x264 at a good preset is slower than realtime, so a feature
    film would take hours; hardware encoding makes it practical.

    Keeps the source codec family: an HEVC source stays HEVC rather than being
    silently downgraded to H.264.
    """
    codec_out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", "--", src],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    codec = (codec_out[0] if codec_out else "h264").lower()
    family = "hevc" if codec in ("hevc", "h265") else "h264"

    if family in _encoder_cache:
        return _encoder_cache[family]

    chosen = None
    if prefer_hw:
        for args, _name in _HW_CANDIDATES.get(family, []):
            if _encoder_works(args):
                chosen = args + ["-pix_fmt", "yuv420p"]
                break

    if chosen is None:
        # `veryfast` at crf 18 is the sensible software fallback: 2.35x realtime and
        # visually fine for content that is mostly being cut, not archived.
        sw = "libx265" if family == "hevc" else "libx264"
        chosen = ["-c:v", sw, "-preset", "veryfast", "-crf", "18"]

    _encoder_cache[family] = chosen
    return chosen


def shift_mutes(
    mutes: list[tuple[str, float, float]],
    cuts: list[dict],
) -> list[tuple[str, float, float]]:
    """Translate input-timeline mute times to output-timeline times after cuts.

    A mute that falls *inside* a removed range disappears — the audio carrying the word
    is gone with the video. A mute after a cut moves earlier by the total cut duration
    preceding it.
    """
    ordered = sorted(cuts, key=lambda c: c["start"])
    out: list[tuple[str, float, float]] = []
    for ref, s, e in mutes:
        if any(c["start"] <= s and e <= c["end"] for c in ordered):
            continue  # removed along with the video
        removed = sum(
            min(c["end"], s) - c["start"]
            for c in ordered if c["start"] < s
        )
        out.append((ref, s - removed, e - removed))
    return out


def render(
    src: str,
    dest: str,
    mutes: list[tuple[str, float, float]],
    video_cuts: list[dict] | None = None,
    quality: str = "splice",
) -> dict:
    """Produce the filtered file. Returns a stats dict for the run report."""
    video_cuts = sorted(video_cuts or [], key=lambda c: c["start"])
    spans = [(s, e) for _r, s, e in mutes]

    if not video_cuts:
        return _render_audio_only(src, dest, mutes, spans, quality)

    return _render_with_cuts(src, dest, mutes, video_cuts, quality)


def _render_audio_only(src, dest, mutes, spans, quality) -> dict:
    """No video cuts: prefer the byte-exact splice path."""
    if quality == "splice":
        import splice as sp

        info = sp.probe(src)
        ok, reason = sp.can_splice(info)
        if ok:
            tmp = tempfile.mkdtemp(prefix="render_")
            audio = os.path.join(tmp, f"audio.{info.codec}")
            stats = sp.splice_audio(src, spans, audio, info=info)
            _run([_tool("ffmpeg"), "-v", "error", "-y", "-i", src, "-i", audio,
                  "-map", "0:v", "-map", "1:a", "-map", "0:s?",
                  "-c", "copy", "-shortest", dest])
            total = stats["bytes_reencoded"] + stats["bytes_copied"]
            return {
                "mode": "splice", "audio_codec": info.codec,
                "bytes_reencoded": stats["bytes_reencoded"],
                "pct_reencoded": round(100.0 * stats["bytes_reencoded"] / max(1, total), 3),
                "summary": (f"splice, {stats['bytes_reencoded']:,} of {total:,} bytes "
                            f"re-encoded; remainder byte-identical"),
            }
        quality = "lossless"

    codec_args = _full_encode_args(src, quality)
    expr = "+".join(f"between(t,{s:.4f},{e:.4f})" for s, e in spans)
    _run([_tool("ffmpeg"), "-v", "error", "-y", "-i", src,
          "-af", f"volume=0:enable='{expr}'",
          "-c:v", "copy", *codec_args, "-c:s", "copy", "-map", "0", dest])
    return {"mode": quality, "summary": f"full audio re-encode ({quality})"}


def _full_encode_args(src: str, quality: str) -> list[str]:
    """Audio args for a whole-track re-encode, respecting the lossless-source rule."""
    import splice as sp

    info = sp.probe(src)
    ok, _reason = sp.can_splice(info)
    if quality == "lossless" or not ok:
        return ["-c:a", "flac", "-compression_level", "8"]
    args = ["-strict", "-2", "-c:a", sp.SPLICEABLE[info.codec]]
    if info.bit_rate:
        args += ["-b:a", info.bit_rate]
    return args


def _render_with_cuts(src, dest, mutes, cuts, quality) -> dict:
    """Remove video ranges and apply audio mutes in one pass.

    Uses the `select`/`aselect` filters with a keep-expression built from the complement
    of the cut ranges, then `setpts`/`asetpts` to close the gaps. This re-encodes video —
    unavoidable, since removing arbitrary (non-keyframe) ranges cannot be done by copy.
    """
    keep = "+".join(
        f"between(t,{a:.4f},{b:.4f})" for a, b in _complement(cuts)
    ) or "1"

    shifted = shift_mutes(mutes, cuts)
    mute_expr = "+".join(f"between(t,{s:.4f},{e:.4f})" for _r, s, e in shifted)

    vf = f"select='{keep}',setpts=N/FRAME_RATE/TB"
    af = f"aselect='{keep}',asetpts=N/SR/TB"
    if mute_expr:
        af += f",volume=0:enable='{mute_expr}'"

    codec_args = _full_encode_args(src, quality)
    venc = _video_encoder(src)
    _run([_tool("ffmpeg"), "-v", "error", "-y", "-i", src,
          "-vf", vf, "-af", af, *venc, *codec_args, dest])

    removed = sum(c["end"] - c["start"] for c in cuts)
    return {
        "mode": f"video-cut+{quality}",
        "cuts": len(cuts),
        "seconds_removed": round(removed, 3),
        "mutes_dropped": len(mutes) - len(shifted),
        "video_encoder": venc[1],
        "summary": (f"{len(cuts)} video cuts removing {removed:.1f}s; "
                    f"video re-encoded with {venc[1]}"),
    }


def _complement(cuts: list[dict], end: float = 1e9) -> list[tuple[float, float]]:
    """Ranges to KEEP, i.e. the gaps between cuts."""
    keep, cursor = [], 0.0
    for c in cuts:
        if c["start"] > cursor:
            keep.append((cursor, c["start"]))
        cursor = max(cursor, c["end"])
    keep.append((cursor, end))
    return keep
