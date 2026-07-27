"""
Scene-cut detection, for snapping video filter ranges to real shot boundaries.

VidAngel's `audiovisual` tags are the same 6-second buckets as the audio ones, but
measurement showed their video taggers were largely marking **shot boundaries**: 6 of 10
buckets in the sample tag-set sat within ~1.2s of a detected cut, four of them inside
350 ms. So snapping a bucket to the nearest cut recovers near-exact edges — the video
analogue of energy refinement for audio.

For buckets with no nearby cut (mid-shot tags), fall back to padding, which is what the
user was doing by hand.
"""

from __future__ import annotations

import bisect
import os
import subprocess
from dataclasses import dataclass

from align import _tool


def detect_cuts(video: str, threshold: float = 0.35, scale: int = 320,
                progress=None) -> list[float]:
    """Timestamps of detected scene changes, ascending.

    One pass over the video, no GPU. `threshold` is ffmpeg's `scene` score: lower finds
    more (and more spurious) cuts; 0.35 was verified to find real shot boundaries on
    1080p live-action TV.

    Passes the file as a normal input rather than via lavfi's `movie=` source. `movie=`
    requires escaping the path *inside* a filter-graph string, and on Windows the drive
    colon defeats it — ffmpeg parsed `C\\:/Users/...` as the filename `C` and returned
    zero cuts silently. A plain `-i` has no such problem.

    Downscaling to `scale` px wide first makes this several times faster with no
    meaningful loss in cut detection, which only needs gross frame differences.

    `progress(seconds, cuts_found)` is called as detection advances. Output is streamed
    rather than collected at the end because this pass is silent for minutes on a feature
    — 25 minutes of CPU on an 87-minute film — and a caller with no progress signal cannot
    tell it apart from a hang.
    """
    vf = f"scale={scale}:-2,select='gt(scene,{threshold})',showinfo"
    proc = subprocess.Popen(
        [_tool("ffmpeg"), "-v", "info", "-nostats", "-i", video,
         "-an", "-sn", "-vf", vf, "-f", "null", os.devnull],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        errors="replace", bufsize=1,
    )

    cuts: list[float] = []
    assert proc.stderr is not None
    for line in proc.stderr:
        if "pts_time:" not in line:
            continue
        tail = line.split("pts_time:", 1)[1]
        token = tail.split()[0].rstrip(",")
        try:
            cuts.append(float(token))
            if progress:
                progress(cuts[-1], len(cuts))
        except ValueError:
            continue

    # stderr has been consumed by the loop above, so wait for the exit code rather than
    # trying to read it again. ffmpeg exits 255 at end-of-stream on this filter graph even
    # on success, so only other non-zero codes are real failures.
    proc.wait()
    if not cuts and proc.returncode not in (0, 255):
        raise RuntimeError(
            f"scene detection failed (rc={proc.returncode}) — check that the file is "
            f"readable and is a video: {video}"
        )
    return sorted(cuts)


@dataclass
class VideoRange:
    """A resolved video cut range."""
    start: float
    end: float
    method: str          # how each edge was decided
    approx_start: float
    approx_end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _nearest(cuts: list[float], t: float, tol: float) -> float | None:
    """Closest cut to `t` within `tol`, or None."""
    if not cuts:
        return None
    i = bisect.bisect_left(cuts, t)
    best, dist = None, tol
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(cuts):
            d = abs(cuts[j] - t)
            if d <= dist:
                best, dist = cuts[j], d
    return best


def snap_range(
    approx_start: float,
    approx_end: float,
    cuts: list[float],
    tolerance: float = 2.0,
    pad: float = 1.5,
    duration: float | None = None,
) -> VideoRange:
    """Resolve an approximate video bucket to a concrete cut range.

    Each edge is snapped to a nearby shot boundary when one exists, else padded. Edges
    are resolved independently, so a range can be shot-accurate on one side and padded
    on the other — common when a tag starts on a cut but ends mid-shot.

    Snapping moves **outward** in preference: a start snaps to a cut at or before the
    bucket, an end to a cut at or after it, so the filtered range never lands inside the
    content it is meant to remove.
    """
    before = [c for c in cuts if c <= approx_start + tolerance]
    after = [c for c in cuts if c >= approx_end - tolerance]

    s_cut = _nearest([c for c in before if c >= approx_start - tolerance],
                     approx_start, tolerance)
    e_cut = _nearest([c for c in after if c <= approx_end + tolerance],
                     approx_end, tolerance)

    if s_cut is not None:
        start, s_method = min(s_cut, approx_start), "cut"
    else:
        start, s_method = approx_start - pad, "pad"

    if e_cut is not None:
        end, e_method = max(e_cut, approx_end), "cut"
    else:
        end, e_method = approx_end + pad, "pad"

    start = max(0.0, start)
    if duration is not None:
        end = min(duration, end)

    return VideoRange(
        start=start, end=end,
        method=f"{s_method}/{e_method}",
        approx_start=approx_start, approx_end=approx_end,
    )


def merge_ranges(ranges: list[VideoRange], gap: float = 0.5) -> list[VideoRange]:
    """Merge ranges that overlap or nearly touch.

    Adjacent VidAngel buckets frequently describe one continuous scene; cutting them
    separately would leave a fragment of the very content being removed.
    """
    out: list[VideoRange] = []
    for r in sorted(ranges, key=lambda x: x.start):
        if out and r.start <= out[-1].end + gap:
            prev = out[-1]
            out[-1] = VideoRange(
                start=prev.start,
                end=max(prev.end, r.end),
                method=f"{prev.method}+merged",
                approx_start=prev.approx_start,
                approx_end=max(prev.approx_end, r.approx_end),
            )
        else:
            out.append(r)
    return out
