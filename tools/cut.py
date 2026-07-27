"""
Excise nudity ranges from a video entirely, rather than obscuring them.

`blur.py` keeps the scene and destroys the region. This module removes the time range —
the right choice when the content is unsalvageable, and what the user does by hand today.

Three things make this different from the audio muting in `run_filter.py`, and each drives
a decision here:

1. **Removal shortens the file.** Muting is in-place, so timestamps stay valid throughout.
   Cutting invalidates every timestamp after the first cut, so ranges are always resolved
   against the *source* timeline and applied in one pass.
2. **A cut is visible; a mute is not.** A range that starts mid-shot produces a jump cut.
   `scenes.detect_cuts` finds real shot boundaries and edges snap outward onto them where
   they exist. Where they don't — a continuous take — there is no honest way to hide the
   join, and the range is padded instead.
3. **Gaps between ranges are usually not worth keeping.** Two nudity ranges 1.5s apart
   leave a fragment too short to register as a scene and which usually still contains the
   content at its edges. `merge_gap` folds them together.

Detection sampling is deliberately denser than `nudity.scan_video`'s default: a missed
frame here means the content survives the cut, and the scan is cheap relative to encoding.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from align import _tool, probe_duration
from nudity import DEFAULT_CLASSES, MIN_SCORE, NudityRange, scan_video
from scenes import detect_cuts


@dataclass
class CutRange:
    """A resolved range to remove, in source-timeline seconds."""

    start: float
    end: float
    method: str = "pad"
    classes: tuple[str, ...] = ()
    peak: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start


def resolve_cuts(
    ranges: list[NudityRange],
    cuts: list[float],
    tolerance: float = 2.0,
    pad: float = 1.0,
    merge_gap: float = 3.0,
    duration: float | None = None,
) -> list[CutRange]:
    """Turn detected nudity ranges into concrete, merged, non-overlapping cut ranges.

    Edges snap outward to a shot boundary within `tolerance`, else pad by `pad`. Snapping
    is outward-only: an inward snap would leave the first or last frames of the content in
    the output, which defeats the point.
    """
    from scenes import _nearest

    resolved: list[CutRange] = []
    for r in ranges:
        before = [c for c in cuts if c <= r.start]
        after = [c for c in cuts if c >= r.end]
        s_cut = _nearest(before, r.start, tolerance) if before else None
        e_cut = _nearest(after, r.end, tolerance) if after else None

        start = s_cut if s_cut is not None else r.start - pad
        end = e_cut if e_cut is not None else r.end + pad
        method = (f"{'cut' if s_cut is not None else 'pad'}/"
                  f"{'cut' if e_cut is not None else 'pad'}")

        start = max(0.0, start)
        if duration is not None:
            end = min(duration, end)
        resolved.append(CutRange(
            start=start, end=end, method=method,
            classes=tuple(r.classes), peak=r.peak.score if r.peak else 0.0,
        ))

    return merge_cuts(resolved, gap=merge_gap)


def merge_cuts(ranges: list[CutRange], gap: float = 3.0) -> list[CutRange]:
    """Merge ranges that overlap or are separated by less than `gap`."""
    out: list[CutRange] = []
    for r in sorted(ranges, key=lambda x: x.start):
        if out and r.start <= out[-1].end + gap:
            prev = out[-1]
            out[-1] = CutRange(
                start=prev.start,
                end=max(prev.end, r.end),
                method=f"{prev.method}+merged",
                classes=tuple(sorted(set(prev.classes) | set(r.classes))),
                peak=max(prev.peak, r.peak),
            )
        else:
            out.append(r)
    return out


def keep_segments(
    cuts: list[CutRange],
    duration: float,
    min_keep: float = 0.5,
) -> list[tuple[float, float]]:
    """Invert cut ranges into the segments to keep.

    Segments shorter than `min_keep` are dropped: a sub-second sliver between two cuts
    reads as a flash frame, not as content, and each kept segment costs a re-encoded join.
    """
    keeps: list[tuple[float, float]] = []
    pos = 0.0
    for c in sorted(cuts, key=lambda x: x.start):
        if c.start > pos:
            keeps.append((pos, min(c.start, duration)))
        pos = max(pos, c.end)
    if pos < duration:
        keeps.append((pos, duration))
    return [(s, e) for s, e in keeps if e - s >= min_keep]


def render_cut(
    video: str,
    keeps: list[tuple[float, float]],
    dest: str,
    crf: int = 18,
    preset: str = "medium",
    audio_bitrate: str = "192k",
) -> str:
    """Concatenate the kept segments into `dest` using a single filter-graph pass.

    Uses `trim`/`atrim` + `concat` inside one ffmpeg invocation rather than writing
    per-segment temp files and demuxer-concatenating them. One pass means one decode and
    no intermediate generation loss, and it sidesteps the demuxer's requirement that every
    segment share identical codec parameters.

    Re-encoding is unavoidable: cutting at arbitrary (non-keyframe) times means the first
    frames of each kept segment have no preceding reference frame. Stream-copying instead
    would force every cut onto a keyframe, moving edges by up to several seconds.
    """
    if not keeps:
        raise ValueError("nothing left to keep — every segment was cut")

    parts = []
    for i, (s, e) in enumerate(keeps):
        parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
    streams = "".join(f"[v{i}][a{i}]" for i in range(len(keeps)))
    graph = ";".join(parts) + f";{streams}concat=n={len(keeps)}:v=1:a=1[outv][outa]"

    args = [
        _tool("ffmpeg"), "-v", "error", "-y", "-i", video,
        "-filter_complex", graph,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", audio_bitrate,
        dest,
    ]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"cut render failed: {proc.stderr.strip()[:800]}")
    return dest


def cut_video(
    video: str,
    dest: str,
    classes: tuple[str, ...] = DEFAULT_CLASSES,
    min_score: float = MIN_SCORE,
    sample_fps: float = 4.0,
    min_hits: int = 2,
    group_gap: float = 3.0,
    merge_gap: float = 3.0,
    pad: float = 1.0,
    tolerance: float = 2.0,
    scene_threshold: float = 0.35,
    min_keep: float = 0.5,
    verify: bool = True,
    progress=None,
) -> dict:
    """Detect nudity in `video` and write a copy with those ranges removed.

    Returns a summary: the cut ranges with how each edge was decided, the kept segments,
    durations before and after, and — when `verify` is set — a re-scan of the output.
    """
    duration = probe_duration(video)
    shot_cuts = detect_cuts(video, threshold=scene_threshold)

    found = scan_video(
        video, sample_fps=sample_fps, classes=classes, min_score=min_score,
        min_hits=min_hits, progress=progress,
    )
    # scan_video groups with its own default; regroup at the caller's gap.
    from nudity import group_detections

    detections = [d for r in found for d in r.detections]
    found = group_detections(detections, gap=group_gap, min_hits=min_hits)

    cuts = resolve_cuts(found, shot_cuts, tolerance=tolerance, pad=pad,
                        merge_gap=merge_gap, duration=duration)
    keeps = keep_segments(cuts, duration, min_keep=min_keep)

    render_cut(video, keeps, dest)

    report = {
        "source_duration": round(duration, 2),
        "output_duration": round(sum(e - s for s, e in keeps), 2),
        "removed": round(sum(c.duration for c in cuts), 2),
        "shot_cuts_found": len(shot_cuts),
        "cuts": [
            {"start": round(c.start, 2), "end": round(c.end, 2),
             "duration": round(c.duration, 2), "edges": c.method,
             "classes": list(c.classes), "peak_score": round(c.peak, 2)}
            for c in cuts
        ],
        "keeps": [[round(s, 2), round(e, 2)] for s, e in keeps],
        "output": dest,
    }

    if verify:
        report["verify"] = verify_output(dest, classes=classes, min_score=min_score,
                                         sample_fps=sample_fps)
    return report


def verify_output(
    dest: str,
    classes: tuple[str, ...] = DEFAULT_CLASSES,
    min_score: float = MIN_SCORE,
    sample_fps: float = 4.0,
) -> dict:
    """Re-scan the rendered output for anything that survived the cut.

    Unlike blurring — where the model keeps firing on body shape through the mosaic — a
    cut either removed the content or it did not, so any detection here is a genuine
    survivor and the count should be zero.
    """
    survivors = scan_video(dest, sample_fps=sample_fps, classes=classes,
                           min_score=min_score, min_hits=1)
    return {
        "clean": not survivors,
        "surviving_ranges": [
            {"start": round(r.start, 2), "end": round(r.end, 2),
             "classes": r.classes, "peak_score": round(r.peak.score, 2) if r.peak else 0.0}
            for r in survivors
        ],
    }
