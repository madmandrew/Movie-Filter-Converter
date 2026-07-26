"""
Surgical splice muting: re-encode ONLY the audio frames overlapping a mute and stream
copy everything else.

Why this exists: a full-track re-encode touches 100% of the audio to change <1% of it.
Splicing keeps the untouched regions **bit-identical** to the source (measured
`PSNR inf dB` on all channels) at essentially no size cost, and the only re-encoded
audio is the silence we are inserting anyway — so nothing of value is lost at all.

Two hard constraints, both found by measurement:

1. **Cut points must land on exact codec frame boundaries.** Letting ffmpeg round `-ss`
   / `-t` produced **+28.46 ms** of drift on a single splice, which accumulates across
   mutes and progressively desyncs audio from video. `frame_duration()` derives the true
   frame length (DTS here: 512 samples @ 48 kHz = 10.6667 ms) and all boundaries are
   snapped to multiples of it.
2. **The muted span must be re-encodable to the SOURCE codec.** A single audio stream
   cannot change codec mid-file, so lossless/object formats with no usable encoder
   (DTS-HD MA, DTS:X, TrueHD+Atmos) cannot be spliced — `can_splice()` rejects them and
   the caller should fall back to a full FLAC re-encode.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from dataclasses import dataclass

from align import _tool

#: Codecs whose muted segment can be re-encoded to the same codec, making splice
#: viable. Maps container codec name -> ffmpeg encoder name.
SPLICEABLE = {
    "dts": "dca",      # plain DTS core only - NOT DTS-HD MA / DTS:X (see can_splice)
    "ac3": "ac3",
    "eac3": "eac3",
    "aac": "aac",
}

#: Profiles that are lossless or object-based; no encoder can reproduce them.
_NO_ENCODER = ("dts-hd", "dts:x", "atmos", "truehd", "mlp")


@dataclass
class AudioInfo:
    codec: str
    profile: str
    channels: int
    sample_rate: int
    bit_rate: str | None
    frame_samples: int

    @property
    def frame_duration(self) -> float:
        """Exact seconds per audio frame — the splice quantum."""
        return self.frame_samples / self.sample_rate


def probe(video: str) -> AudioInfo:
    """Audio stream parameters, including the true frame size in samples."""
    out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,profile,channels,sample_rate,bit_rate",
         "-of", "default=noprint_wrappers=1", "--", video],
        capture_output=True, text=True, check=True,
    ).stdout
    v = dict(l.split("=", 1) for l in out.strip().splitlines() if "=" in l)

    # nb_samples comes from a decoded frame; read a couple of seconds in to avoid any
    # oddity at the very start of the stream.
    fr = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "a:0",
         "-read_intervals", "60%+0.5", "-show_frames",
         "-show_entries", "frame=nb_samples", "-of", "csv=p=0", "--", video],
        capture_output=True, text=True,
    ).stdout.split()
    samples = next((int(x) for x in fr if x.strip().isdigit()), 0)

    sr = int(v.get("sample_rate") or 48000)
    br = v.get("bit_rate", "N/A")
    return AudioInfo(
        codec=v.get("codec_name", "unknown"),
        profile=v.get("profile", "unknown"),
        channels=int(v.get("channels") or 0),
        sample_rate=sr,
        bit_rate=br if br.isdigit() else None,
        # Fall back to a sane DTS-ish default rather than dividing by zero.
        frame_samples=samples or 512,
    )


def can_splice(info: AudioInfo) -> tuple[bool, str]:
    """(viable, reason). Splice needs a same-codec encoder for the muted segment."""
    prof = (info.profile or "").lower()
    if info.codec in ("truehd", "mlp") or any(k in prof for k in _NO_ENCODER):
        return False, (
            f"{info.profile} is lossless/object-based and has no usable encoder; the "
            f"muted segment would have to become FLAC, and one audio stream cannot "
            f"change codec mid-file"
        )
    if info.codec not in SPLICEABLE:
        return False, f"no known same-codec encoder for '{info.codec}'"
    return True, f"{info.codec} @ {info.frame_duration*1000:.4f} ms/frame"


#: Extra frames of silence placed either side of a mute. Replacing whole frames still
#: leaves audible decoder ringing at the seams — measured ~40 RMS against 830 in the
#: source (95% reduction, but not silent). Lossy transform codecs overlap adjacent
#: frames, so a word's energy bleeds one frame past its own boundary. Two frames
#: (~21 ms at 48 kHz) absorbs it without meaningfully widening the mute.
EDGE_FRAMES = 2


def snap(t: float, frame: float, mode: str, pad_frames: int = 0) -> float:
    """Snap a time to a frame boundary. `mode` is 'floor' or 'ceil'.

    Mute boundaries always move OUTWARD (start floors, end ceils) so snapping can only
    ever cover more of the target word, never less. `pad_frames` extends further out.
    """
    n = t / frame
    if mode == "floor":
        return max(0.0, (math.floor(n) - pad_frames) * frame)
    return (math.ceil(n) + pad_frames) * frame


def plan_segments(
    mutes: list[tuple[float, float]],
    duration: float,
    frame: float,
) -> list[tuple[float, float, bool]]:
    """Build a frame-aligned (start, end, is_muted) segment list covering [0, duration].

    Overlapping or adjacent mutes are merged first — two segments that share a boundary
    would otherwise produce a zero-length copy segment that ffmpeg rejects.
    """
    snapped = sorted(
        (snap(s, frame, "floor", EDGE_FRAMES), snap(e, frame, "ceil", EDGE_FRAMES))
        for s, e in mutes
    )
    merged: list[list[float]] = []
    for s, e in snapped:
        if merged and s <= merged[-1][1] + frame / 2:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    segs: list[tuple[float, float, bool]] = []
    cursor = 0.0
    for s, e in merged:
        s = max(0.0, s)
        e = min(duration, e)
        if s > cursor:
            segs.append((cursor, s, False))
        segs.append((s, e, True))
        cursor = e
    if cursor < duration:
        segs.append((cursor, duration, False))
    return [(s, e, m) for s, e, m in segs if e - s > 1e-9]


def _frame_bytes(data: bytes, info: AudioInfo) -> tuple[int | None, int]:
    """(bytes per frame, trailing remainder) if frames are fixed-size, else (None, 0).

    Byte-offset splicing is only valid for constant-bitrate streams. Derive the frame
    size from the expected frame count and verify it divides the stream exactly.
    """
    if info.bit_rate:
        expected = int(info.bit_rate) * info.frame_duration / 8
        candidate = int(round(expected))
        for size in (candidate, candidate - 1, candidate + 1):
            if size > 0 and len(data) % size == 0:
                return size, 0
    # Fall back: try common DTS/AC3 frame sizes.
    for size in (2012, 2013, 1792, 1536, 1280):
        if len(data) % size == 0:
            return size, 0
    return None, 0


def _encode_silence(tmp: str, info: AudioInfo, encoder: str, frames: int) -> bytes:
    """Raw encoded silence, at least `frames` frames long, same codec/bitrate."""
    seconds = max(0.05, frames * info.frame_duration + 4 * info.frame_duration)
    out = os.path.join(tmp, f"sil_{frames}.raw")
    if not os.path.exists(out):
        args = [
            _tool("ffmpeg"), "-v", "error", "-y",
            "-f", "lavfi", "-i",
            f"anullsrc=r={info.sample_rate}:cl={info.channels}c",
            "-t", f"{seconds:.6f}", "-strict", "-2", "-c:a", encoder,
        ]
        if info.bit_rate:
            args += ["-b:a", info.bit_rate]
        args += ["-f", info.codec, out]
        subprocess.run(args, check=True, capture_output=True)
    return open(out, "rb").read()


def splice_audio(
    video: str,
    mutes: list[tuple[float, float]],
    dest: str,
    info: AudioInfo | None = None,
    workdir: str | None = None,
) -> dict:
    """Write a muted audio-only file by copy/re-encode/copy splicing.

    Returns stats: byte counts, how much was re-encoded, and the frame alignment used.
    """
    info = info or probe(video)
    ok, reason = can_splice(info)
    if not ok:
        raise ValueError(f"cannot splice: {reason}")

    frame = info.frame_duration
    encoder = SPLICEABLE[info.codec]

    dur = float(subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", "--", video], capture_output=True, text=True, check=True,
    ).stdout.strip())

    tmp = workdir or tempfile.mkdtemp(prefix="splice_")
    os.makedirs(tmp, exist_ok=True)

    # Demux the audio once, stream copied. Working from a standalone audio file keeps
    # the segment cuts away from video interleaving.
    ext = "mka"
    base = os.path.join(tmp, f"base.{ext}")
    subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-y", "-i", video, "-vn", "-sn",
         "-map", "0:a:0", "-c:a", "copy", base],
        check=True, capture_output=True,
    )

    segs = plan_segments(mutes, dur, frame)

    # Splice by BYTE OFFSET on the raw elementary stream, not by timestamp on a
    # container.
    #
    # Trimming each segment to its own container file and concatenating loses ~1 ms per
    # segment (each trim rounds independently). That accumulated to 24 ms across one
    # episode and — worse — progressively slid the mutes off their targets until the
    # last one leaked audible speech (RMS 37.6 instead of 0).
    #
    # Constant-bitrate codecs like DTS have fixed-size frames (measured: 2012 bytes,
    # 512 samples, and the whole 241 MB stream divides into 119,855 frames with **zero
    # remainder**). So frame N always starts at byte N*frame_bytes, and cutting by byte
    # offset is exact by construction — no timestamps, nothing to round, zero drift.
    raw = os.path.join(tmp, "base.raw")
    subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-y", "-i", base, "-c:a", "copy",
         "-f", info.codec, raw],
        check=True, capture_output=True,
    )
    data = open(raw, "rb").read()

    frame_bytes, remainder = _frame_bytes(data, info)
    if frame_bytes is None:
        raise ValueError(
            "audio frames are not fixed-size, so byte-exact splicing is unsafe; "
            "use a full re-encode instead"
        )

    reencoded = copied = 0
    chunks: list[bytes] = []
    for s, e, muted in segs:
        i0 = int(round(s / frame)) * frame_bytes
        i1 = min(len(data) - remainder, int(round(e / frame)) * frame_bytes)
        if muted:
            need = i1 - i0
            n_frames = need // frame_bytes
            sil = _encode_silence(tmp, info, encoder, n_frames)
            # Take whole frames only, and repeat the silence if it came up short, so the
            # replacement is exactly `need` bytes and frame alignment is preserved.
            usable = (len(sil) // frame_bytes) * frame_bytes
            if usable == 0:
                raise ValueError("silence encoder produced no complete frames")
            block = sil[:usable]
            filler = (block * (need // usable + 1))[:need]
            chunks.append(filler)
            reencoded += need
        else:
            chunks.append(data[i0:i1])
            copied += i1 - i0
    if remainder:
        chunks.append(data[len(data) - remainder:])

    spliced_raw = os.path.join(tmp, "spliced.raw")
    with open(spliced_raw, "wb") as fh:
        for c in chunks:
            fh.write(c)

    subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-y", "-f", info.codec, "-i", spliced_raw,
         "-c:a", "copy", dest],
        check=True, capture_output=True,
    )

    out_dur = float(subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", "--", dest], capture_output=True, text=True, check=True,
    ).stdout.strip())

    return {
        "segments": len(segs),
        "muted_segments": sum(1 for *_x, m in segs if m),
        "bytes_reencoded": reencoded,
        "bytes_copied": copied,
        "frame_ms": frame * 1000,
        "source_duration": dur,
        "output_duration": out_dur,
        "drift_ms": (out_dur - dur) * 1000,
        "workdir": tmp,
    }
