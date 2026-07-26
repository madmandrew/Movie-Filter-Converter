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
#:
#: Listing an encoder in `ffmpeg -encoders` does NOT mean it works. On the development
#: laptop `h264_nvenc` is listed but fails at runtime (the NVIDIA driver exposes nvenc
#: API 13.0 while the ffmpeg build wants 13.1), while on the Unraid deployment target — a
#: GTX 1070/1080 with current drivers — NVENC is the *best* option and QSV is absent
#: entirely. So the order below is a preference, not a ranking of one machine's hardware,
#: and `_encoder_works()` decides what is actually usable on the host.
#:
#: NVENC is listed first because a discrete NVIDIA card is the expected server
#: configuration; QSV second because it is what this laptop has; AMF for completeness.
#:
#: Speeds measured on 60 s of 1080p on the laptop (Intel Iris Xe / no working NVENC):
#:   h264_qsv                  9.1 s  = 6.62x realtime, 39.2 MB
#:   libx264 veryfast         25.6 s  = 2.35x realtime, 53.5 MB
#:   libx264 medium           77.5 s  = 0.77x realtime, 60.0 MB
#:   libx264 slow            127.3 s  = 0.47x realtime, 70.4 MB   <- unusable
#: Pascal-generation NVENC should land in the same order of magnitude as QSV.
#:
#: Note: Pascal (10-series) NVENC does **not** support HEVC 10-bit B-frames and has no
#: AV1 encoder, but for 8-bit H.264/HEVC re-encodes it is fine.
_HW_CANDIDATES = {
    "h264": [
        (["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20",
          "-b:v", "0"], "h264_nvenc"),
        (["-c:v", "h264_qsv", "-global_quality", "20"], "h264_qsv"),
        (["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "20",
          "-qp_p", "20"], "h264_amf"),
    ],
    "hevc": [
        (["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "22",
          "-b:v", "0"], "hevc_nvenc"),
        (["-c:v", "hevc_qsv", "-global_quality", "22"], "hevc_qsv"),
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


def audio_track_count(src: str) -> int:
    out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", "--", src],
        capture_output=True, text=True,
    ).stdout
    return len([l for l in out.splitlines() if l.strip()])


def _render_audio_only(src, dest, mutes, spans, quality) -> dict:
    """No video cuts: prefer the byte-exact splice path.

    Multi-track files are common in this library (one movie has 7 audio tracks:
    commentary, other languages, an AC3 compatibility track). Every track must be
    filtered — mapping only `a:0` would leave an *unfiltered* track in the output that a
    player could select, defeating the whole point. The splice path handles one stream,
    so multi-track files take the filter-graph path where each track gets its own
    `volume` filter.
    """
    n_audio = audio_track_count(src)

    if quality == "splice" and n_audio == 1:
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
                "mode": "splice", "audio_codec": info.codec, "audio_tracks": 1,
                "bytes_reencoded": stats["bytes_reencoded"],
                "pct_reencoded": round(100.0 * stats["bytes_reencoded"] / max(1, total), 3),
                "summary": (f"splice, {stats['bytes_reencoded']:,} of {total:,} bytes "
                            f"re-encoded; remainder byte-identical"),
            }
        quality = "lossless"

    codec_args = _full_encode_args(src, quality)
    expr = "+".join(f"between(t,{s:.4f},{e:.4f})" for s, e in spans)

    if n_audio <= 1:
        _run([_tool("ffmpeg"), "-v", "error", "-y", "-i", src,
              "-af", f"volume=0:enable='{expr}'",
              "-c:v", "copy", *codec_args, "-c:s", "copy", "-map", "0", dest])
        note = ""
    else:
        # One filter chain per audio stream, so no track escapes filtering.
        chains = ";".join(
            f"[0:a:{i}]volume=0:enable='{expr}'[fa{i}]" for i in range(n_audio)
        )
        maps: list[str] = ["-map", "0:v"]
        for i in range(n_audio):
            maps += ["-map", f"[fa{i}]"]
        maps += ["-map", "0:s?"]
        _run([_tool("ffmpeg"), "-v", "error", "-y", "-i", src,
              "-filter_complex", chains, *maps,
              "-c:v", "copy", *codec_args, "-c:s", "copy", dest])
        note = f" ({n_audio} audio tracks, all filtered)"

    return {"mode": quality, "audio_tracks": n_audio,
            "summary": f"full audio re-encode ({quality}){note}"}


def _audio_streams(src: str) -> list[dict]:
    """Per-stream codec/bitrate for every audio track."""
    out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index,codec_name,profile,bit_rate,channels",
         "-of", "json", "--", src],
        capture_output=True, text=True, check=True,
    ).stdout
    import json as _json

    return _json.loads(out).get("streams", [])


def _full_encode_args(src: str, quality: str) -> list[str]:
    """Audio args for a whole-track re-encode, respecting the lossless-source rule.

    Emits **per-stream** codec options. A single `-c:a` would apply one codec to every
    track, which silently converted a stereo AC3 commentary track into DTS in testing.
    Each track keeps its own codec and bitrate.
    """
    import splice as sp

    streams = _audio_streams(src) or [{}]
    args: list[str] = ["-strict", "-2"]

    for i, st in enumerate(streams):
        codec = (st.get("codec_name") or "").lower()
        profile = (st.get("profile") or "").lower()
        lossless = codec in ("truehd", "mlp") or any(
            k in profile for k in sp._NO_ENCODER
        )
        if quality == "lossless" or lossless or codec not in sp.SPLICEABLE:
            args += [f"-c:a:{i}", "flac", f"-compression_level:a:{i}", "8"]
            continue
        args += [f"-c:a:{i}", sp.SPLICEABLE[codec]]
        br = st.get("bit_rate")
        if br and str(br).isdigit():
            args += [f"-b:a:{i}", str(br)]
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

    # Mutes are applied BEFORE the cut selection, in input-timeline coordinates, and the
    # `keep` selection then removes frames. Applying them after would require the shifted
    # times — which is what `shift_mutes` computes for reporting, but doing it in one
    # chain is simpler and avoids a second timeline translation.
    n_audio = audio_track_count(src)
    a_chain = f"aselect='{keep}',asetpts=N/SR/TB"
    if mute_expr:
        a_chain = f"volume=0:enable='{mute_expr}'," + a_chain

    chains = [f"[0:v]select='{keep}',setpts=N/FRAME_RATE/TB[fv]"]
    maps = ["-map", "[fv]"]
    for i in range(max(1, n_audio)):
        chains.append(f"[0:a:{i}]{a_chain}[fa{i}]")
        maps += ["-map", f"[fa{i}]"]
    maps += ["-map", "0:s?"]

    codec_args = _full_encode_args(src, quality)
    venc = _video_encoder(src)
    _run([_tool("ffmpeg"), "-v", "error", "-y", "-i", src,
          "-filter_complex", ";".join(chains), *maps,
          *venc, *codec_args, "-c:s", "copy", dest])

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
