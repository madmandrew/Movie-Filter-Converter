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
import shutil
import subprocess
import tempfile

import align as _align
from align import _tool


def _run(args: list[str]) -> None:
    _align.run_proc(args, check=True, capture_output=True)


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
#: Note: Pascal (10-series) NVENC **cannot encode HEVC 10-bit at all** (decode is fine)
#: and has no AV1 encoder, but for 8-bit H.264/HEVC re-encodes it is fine. That is why
#: `_encoder_works` probes at the source's real pixel format and why a 10-bit HEVC source
#: is allowed to fall back to an 8-bit hardware encode: the alternative measured 0.31x
#: realtime on the Unraid box, i.e. 2.5 hours for a 47-minute episode.
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


def _encoder_works(args: list[str], pix_fmt: str = "yuv420p") -> bool:
    """Try a short encode of colour bars. Cheap, and catches driver mismatches.

    `pix_fmt` must match the source being encoded. Probing 8-bit and then encoding a
    10-bit file is a false positive in exactly the way a load-only CUDA probe is: Pascal
    NVENC advertises `hevc_nvenc` and encodes 8-bit happily, but **cannot encode HEVC
    10-bit at all**. The 8-bit probe passed, the real encode then fell back to software
    x265 at ~0.3x realtime, and nothing said so — a 47-minute episode became a
    2.5-hour render that looked like a hang.
    """
    try:
        _align.run_proc(
            [_tool("ffmpeg"), "-v", "error", "-y",
             "-f", "lavfi",
             "-i", f"testsrc=size=640x360:rate=24:duration=0.5,format={pix_fmt}",
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
    probe = _align.run_proc(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt", "-of", "csv=p=0", "--", src],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    fields = (probe[0].split(",") if probe else [])
    codec = (fields[0] if fields else "h264").lower()
    pix_fmt = (fields[1] if len(fields) > 1 else "yuv420p").lower()
    family = "hevc" if codec in ("hevc", "h265") else "h264"
    # 10-bit is the case that breaks hardware encoding on older cards, so it decides both
    # what the probe encodes and which candidates are eligible at all.
    ten_bit = "10" in pix_fmt

    key = f"{family}/{pix_fmt}"
    if key in _encoder_cache:
        return _encoder_cache[key]

    chosen = None
    chosen_name = None
    # Preserving bit depth is preferred, but a hardware encoder that only does 8-bit still
    # beats software by 3-10x. Try same-family first, then cross-family (HEVC 10-bit ->
    # H.264 8-bit on Pascal), and only then give up on hardware.
    ladder = list(_HW_CANDIDATES.get(family, []))
    if ten_bit and family == "hevc":
        ladder += _HW_CANDIDATES.get("h264", [])

    if prefer_hw:
        for args, name in ladder:
            # Probe at the source's real depth. An 8-bit-only encoder fails here rather
            # than passing and then falling over on the actual file.
            if _encoder_works(args, pix_fmt):
                chosen, chosen_name = args + ["-pix_fmt", pix_fmt], name
                break
            # Retry 8-bit: the encoder may be usable if the frames are converted down.
            # That is a real quality decision (10-bit -> 8-bit can band gradients), so it
            # is only taken because the alternative is software at a fraction of realtime.
            if ten_bit and _encoder_works(args, "yuv420p"):
                chosen, chosen_name = args + ["-pix_fmt", "yuv420p"], f"{name} (8-bit)"
                break

    if chosen is None:
        # `veryfast` at crf 18 is the sensible software fallback: 2.35x realtime and
        # visually fine for content that is mostly being cut, not archived.
        #
        # x265 is ~3x slower than x264 and, on a 10-bit HEVC source, x264 at 10-bit
        # measures *better* per unit of encode time. Measured on 20s of the real
        # Severance S02E05 (Main 10, yuv420p10le), PSNR/SSIM vs. the source:
        #   libx265 veryfast        1.36x realtime  2.02 MB  PSNR 57.07  SSIM 0.99898
        #   libx264 veryfast 10-bit 3.61x realtime  3.32 MB  PSNR 53.09  SSIM 0.99794
        #   libx264 veryfast  8-bit 4.04x realtime  5.06 MB  PSNR 50.92  SSIM 0.99513
        # x265 wins on quality-per-byte, but 1.36x realtime means ~35 min/episode of
        # pure CPU. x264 10-bit keeps the bit depth, is ~2.7x faster, and the quality gap
        # is far above the visually-lossless threshold. Prefer it, but keep x265 when the
        # caller explicitly wants size over speed.
        if family == "hevc" and ten_bit:
            sw, extra = "libx264", ["-pix_fmt", pix_fmt]
        elif family == "hevc":
            sw, extra = "libx265", []
        else:
            sw, extra = "libx264", []
        chosen = ["-c:v", sw, "-preset", "veryfast", "-crf", "18"] + extra
        chosen_name = f"{sw} veryfast (software)"

    # Say which encoder won. A silent software fallback is indistinguishable from a hang:
    # it turned a ~6-minute render into ~2.5 hours with nothing in the log to explain it,
    # and the only symptom was a .partial file growing slowly.
    print(f"[render] video encoder: {chosen_name} for {codec} {pix_fmt}", flush=True)
    _encoder_cache[key] = chosen
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
    out = _align.run_proc(
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

    splice_note = ""
    if quality == "splice" and n_audio == 1:
        import splice as sp

        # `probe` and `splice_audio` both refuse rather than splice on geometry they
        # cannot verify, and every one of those refusals is recoverable — the filter-graph
        # path below produces a correct file from the same mutes. So a refusal downgrades
        # the run; it must never fail it.
        try:
            info = sp.probe(src)
            ok, reason = sp.can_splice(info)
        except ValueError as exc:
            info, ok, reason = None, False, str(exc)

        if ok:
            # Holds the spliced audio track, so it must go whether the splice succeeds,
            # is declined, or the remux fails — see the note in splice.splice_audio about
            # abandoned scratch filling docker.img.
            tmp = tempfile.mkdtemp(prefix="render_")
            try:
                audio = os.path.join(tmp, f"audio.{info.codec}")
                try:
                    stats = sp.splice_audio(src, spans, audio, info=info)
                except ValueError as exc:
                    ok, reason = False, str(exc)
                else:
                    # No `-shortest` here, ever. The spliced track is a raw elementary
                    # stream with no container duration, so ffmpeg cannot compare its
                    # length against the video and stops early — it truncated 9.4s out of
                    # a 59s sample, and cost Severance S01E01 107s and S01E02 62s of audio
                    # off the end while the video ran on to full length. That asymmetry
                    # (audio short, video long) is the signature: queued video packets
                    # still flush past the cut point, so a truncated file does NOT look
                    # like the usual `-shortest` even-trim.
                    #
                    # The flag protects against nothing here. A mute replaces bytes in
                    # place, so the spliced stream is exactly as long as the source, and
                    # `splice_audio` already refuses outright if it is not (see the length
                    # guard at the end of that function).
                    #
                    # The spliced track arrives as a raw elementary stream with no tags
                    # and no dispositions, so the metadata args below are not cosmetic:
                    # without them the output audio has no `language` and is not
                    # `default`, and players then select no audio track at all. The file
                    # decodes perfectly and still presents as having lost its sound.
                    # `-max_interleave_delta 0`: interleave strictly by timestamp.
                    #
                    # The spliced audio arrives as a raw elementary stream, so ffmpeg
                    # cannot see how long it is and its default interleaving queue —
                    # sized for streams whose durations are known — gives up and flushes
                    # each stream in bulk instead. The output then stores ~37 minutes of
                    # video before the matching audio (measured: worst A/V gap 2232s
                    # against 0.42s in the source, runs of 55,799 consecutive video
                    # packets). Every byte is present and every stream decodes, so no
                    # duration, checksum or metadata check catches it — but a player has
                    # to read gigabytes ahead to pair audio with video, so it buffers
                    # forever. That is the "stuck spinning" symptom, and this flag is
                    # what prevents it: measured 17.9s of seek time down to 2.5s, against
                    # 2.4s for the untouched source.
                    _run([_tool("ffmpeg"), "-v", "error", "-y", "-i", src, "-i", audio,
                          "-map", "0:v", "-map", "1:a", "-map", "0:s?",
                          "-c", "copy",
                          "-max_interleave_delta", "0",
                          *_audio_metadata_args(src, 1),
                          dest])
                    _assert_not_truncated(src, dest)
                    total = stats["bytes_reencoded"] + stats["bytes_copied"]
                    return {
                        "mode": "splice", "audio_codec": info.codec, "audio_tracks": 1,
                        "bytes_reencoded": stats["bytes_reencoded"],
                        "pct_reencoded": round(
                            100.0 * stats["bytes_reencoded"] / max(1, total), 3),
                        "summary": (f"splice, {stats['bytes_reencoded']:,} of {total:,} "
                                    f"bytes re-encoded; remainder byte-identical"),
                    }
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        # Leave `quality` alone. Forcing "lossless" here turned every unspliceable file
        # into FLAC, including ordinary AC3 that only failed the byte-offset checks —
        # inflating a 448 kbps track and breaking Plex direct play. `_full_encode_args`
        # already routes genuinely lossless sources to FLAC per stream, so the default
        # path re-encodes each track to its own codec at its own bitrate.
        splice_note = f" (splice declined: {reason})"

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
              "-c:v", "copy", *codec_args, "-c:s", "copy",
              *_audio_metadata_args(src, n_audio), dest])
        note = f" ({n_audio} audio tracks, all filtered)"

    return {"mode": quality, "audio_tracks": n_audio,
            "summary": f"full audio re-encode ({quality}){note}{splice_note}"}


#: How far the output may fall short of the source before it is treated as truncated.
#: Container durations disagree by a frame or two between muxes, and a mute never changes
#: length, so anything past this is lost audio rather than rounding.
_TRUNCATION_TOLERANCE = 1.0


def _stream_end(path: str, stream: str) -> float | None:
    """When the last packet of `stream` finishes, in seconds. None if unreadable.

    Read from packets, not `stream=duration`: Matroska reports no per-stream duration
    (both Severance episodes came back `N/A`), so the container-level figure is the only
    one available and it does not say whether a single track ends early.
    """
    out = _align.run_proc(
        [_tool("ffprobe"), "-v", "error", "-select_streams", stream,
         "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0",
         "-read_intervals", "999999%+#1", "--", path],
        capture_output=True, text=True,
    ).stdout
    for line in reversed(out.splitlines()):
        parts = [p for p in line.strip().split(",") if p]
        try:
            return sum(float(p) for p in parts[:2])
        except ValueError:
            continue
    return None


def _assert_not_truncated(src: str, dest: str) -> None:
    """Fail if the rendered file lost audio off the end.

    `-shortest` silently cut 107s off one episode and 62s off another, and every other
    check passed: the byte-splice was exact, its own length guard was satisfied, and the
    run reported success. Nothing compared the *output* against the source, so the damage
    only surfaced on playback. This is that comparison.
    """
    for stream, label in (("a:0", "audio"), ("v:0", "video")):
        src_end = _stream_end(src, stream)
        out_end = _stream_end(dest, stream)
        if src_end is None or out_end is None:
            continue  # nothing to compare against; not evidence of a problem
        if src_end - out_end > _TRUNCATION_TOLERANCE:
            raise ValueError(
                f"rendered {label} ends at {out_end:.1f}s but the source runs to "
                f"{src_end:.1f}s — {src_end - out_end:.1f}s was lost off the end"
            )


#: Stream tags worth carrying onto a rebuilt audio track. `language` is what players
#: select on; `title` is what the user sees ("Commentary"). Everything else on these
#: tracks is mkvmerge bookkeeping (BPS, NUMBER_OF_BYTES, _STATISTICS_*) that describes
#: the *source* bytes and would be a lie on a re-encoded stream.
_CARRIED_TAGS = ("language", "title")


def _audio_metadata_args(src: str, n_audio: int) -> list[str]:
    """Restore language/title tags and dispositions onto rebuilt audio tracks.

    A stream mapped from a `[fa0]` filter label — or from the spliced elementary stream —
    is a *new* stream as far as the muxer is concerned: it inherits neither the source's
    tags nor its dispositions. The result is audio with no `language` and no `default`
    flag, which players read as "no track worth selecting", so the file presents as
    having lost its audio even though every sample is present.

    Set each tag directly with `-metadata:s:a:N`. The obvious-looking
    `-map_metadata:s:a:N 0:s:a:N` is a trap: naming *any* per-stream metadata map
    switches ffmpeg off its default of copying stream metadata, so every stream not
    named — the video and all 34 subtitle tracks of a Clarkson's Farm episode — comes
    out with no language at all. That shipped, and made the file unplayable rather than
    merely silent. Setting the tag leaves the default copying alone.
    """
    args: list[str] = []
    for i in range(n_audio):
        tags = _stream_tags(src, f"a:{i}")
        for key in _CARRIED_TAGS:
            if tags.get(key):
                args += [f"-metadata:s:a:{i}", f"{key}={tags[key]}"]
        args += [f"-disposition:a:{i}", _disposition(src, f"a:{i}")]
    return args


def _stream_tags(src: str, stream: str) -> dict:
    """Tags on `stream`, or an empty dict if it has none / cannot be read."""
    out = _align.run_proc(
        [_tool("ffprobe"), "-v", "error", "-select_streams", stream,
         "-show_entries", "stream_tags", "-of", "json", "--", src],
        capture_output=True, text=True,
    ).stdout
    import json as _json

    try:
        return _json.loads(out)["streams"][0].get("tags") or {}
    except (ValueError, KeyError, IndexError):
        return {}


def _disposition(src: str, stream: str) -> str:
    """The disposition flags set on `stream`, as an ffmpeg `-disposition` value.

    ffmpeg's default for a mapped stream is to *clear* every flag, so a track that was
    `default` in the source silently stops being `default` in the output. Players use
    that flag (with `language`) to choose a track, so losing it reads to the user as
    "the audio is missing" even though the stream is present and decodes fine.

    Returns "0" when nothing is set, which is ffmpeg's spelling for "no flags".
    """
    out = _align.run_proc(
        [_tool("ffprobe"), "-v", "error", "-select_streams", stream,
         "-show_entries", "stream_disposition", "-of", "json", "--", src],
        capture_output=True, text=True,
    ).stdout
    import json as _json

    try:
        disp = _json.loads(out)["streams"][0]["disposition"]
    except (ValueError, KeyError, IndexError):
        return "default"  # the common case; better than clearing the flag outright
    flags = [k for k, v in disp.items() if v]
    return "+".join(flags) if flags else "0"


def _audio_streams(src: str) -> list[dict]:
    """Per-stream codec/bitrate for every audio track."""
    out = _align.run_proc(
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
        # `REENCODE_TO`, not `SPLICEABLE`: a codec can be re-encodable to itself without
        # being byte-spliceable. Gating on the stricter set sent Opus to FLAC, tripling
        # the file size and breaking playback on clients that reject FLAC-in-Matroska.
        if quality == "lossless" or lossless or codec not in sp.REENCODE_TO:
            args += [f"-c:a:{i}", "flac", f"-compression_level:a:{i}", "8"]
            continue
        args += [f"-c:a:{i}", sp.REENCODE_TO[codec]]
        br = st.get("bit_rate")
        if br and str(br).isdigit():
            args += [f"-b:a:{i}", str(br)]
        elif codec == "opus":
            # Matroska reports no per-stream bit_rate for Opus, and libopus without an
            # explicit rate defaults to 96k/channel jammed to its own idea of stereo —
            # an audible downgrade from a ~128k source. Ask for a sane rate per channel
            # instead of letting the default decide.
            ch = st.get("channels") or 2
            args += [f"-b:a:{i}", str(64000 * int(ch))]
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

    # `shifted` is the OUTPUT timeline — what the report needs, and what a viewer
    # scrubbing the finished file would see. It is deliberately NOT what the filter
    # expression below uses.
    shifted = shift_mutes(mutes, cuts)

    # `volume` sits before `aselect` in the chain, so its `enable` expression is evaluated
    # against the INPUT timeline — source times, uncorrected for any cut. Feeding it the
    # shifted times applied the cut correction a second time and every mute after a cut
    # fired early by the total duration cut before it. Severance S01E06 had one 8.4s cut
    # and every later mute landed 8.4s before its word.
    #
    # A mute dropped by shift_mutes (it fell inside a cut) must also be dropped here, or
    # it would silence audio that survives. Recomputed with the same predicate rather than
    # matched against `shifted` by ref: recovered mutes share a ref stem and a span, so a
    # ref set is not a reliable key.
    ordered = sorted(cuts, key=lambda c: c["start"])
    kept = [
        (s, e) for _ref, s, e in mutes
        if not any(c["start"] <= s and e <= c["end"] for c in ordered)
    ]
    mute_expr = "+".join(f"between(t,{s:.4f},{e:.4f})" for s, e in kept)
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
          *venc, *codec_args, "-c:s", "copy",
          *_audio_metadata_args(src, max(1, n_audio)), dest])

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
