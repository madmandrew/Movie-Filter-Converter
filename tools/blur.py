"""
Region blurring for video, using NudeNet box detections.

`nudity.py` finds *when* nudity happens so a scene can be cut. This module handles the
other option: keep the scene, obscure the *region*. That is a different problem in three
ways, and each drives a decision here.

1. **Every frame must be detected, not sampled.** Discovery can sample at 1 fps because a
   scene lasts seconds. A blur cannot: an un-inspected frame is an unblurred frame. So we
   decode the whole span and run the detector on each frame.
2. **Detection flickers, blur must not.** The model's confidence wobbles frame to frame and
   drops boxes entirely for a frame or two mid-scene. Applied literally that is a strobing
   hole in the censoring. `hold` carries a box forward across brief dropouts, and boxes are
   dilated so a slightly-shifted box next frame still covers the same anatomy.
3. **Blur strength has to beat downscaling.** A light blur on a 1080p source is still
   legible when the region is small. Kernel size is derived from region size, not fixed.

Detection runs on downscaled frames (the model works at 320px internally regardless), and
boxes are scaled back up to full resolution for compositing. This keeps a 1080p pass at
roughly the cost of a 640px one.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from align import _tool, probe_fps
from nudity import DEFAULT_CLASSES, MIN_SCORE, _detector


@dataclass
class Box:
    """A detected region in source-resolution pixels."""

    x: int
    y: int
    w: int
    h: int
    cls: str = ""
    score: float = 0.0

    def dilate(self, factor: float, max_w: int, max_h: int) -> "Box":
        """Grow the box about its centre, clamped to frame bounds.

        The detector's boxes hug the anatomy tightly. Blurring exactly that leaves a
        sharp-edged patch whose outline still reads clearly, so pad it out.
        """
        cx, cy = self.x + self.w / 2, self.y + self.h / 2
        nw, nh = self.w * factor, self.h * factor
        x0 = max(0, int(cx - nw / 2))
        y0 = max(0, int(cy - nh / 2))
        x1 = min(max_w, int(cx + nw / 2))
        y1 = min(max_h, int(cy + nh / 2))
        return Box(x0, y0, max(1, x1 - x0), max(1, y1 - y0), self.cls, self.score)


@dataclass
class FrameDetections:
    index: int
    time: float
    boxes: list[Box] = field(default_factory=list)


def detect_all_frames(
    video: str,
    classes: tuple[str, ...] = DEFAULT_CLASSES,
    min_score: float = MIN_SCORE,
    detect_width: int = 640,
    start: float = 0.0,
    end: float | None = None,
    progress=None,
) -> tuple[list[FrameDetections], int, int, float]:
    """Run the detector over every frame, streaming raw video through a pipe.

    Returns (per-frame detections, source width, source height, fps). Boxes are in
    *source* pixel coordinates.

    Frames are piped as raw BGR rather than written out as JPEGs: at 24 fps a minute of
    video is 1440 files, and the disk round-trip dominates the detector's own cost.
    """
    import numpy as np

    w, h = _probe_size(video)
    fps = probe_fps(video)

    # Preserve aspect ratio; the detector letterboxes internally anyway.
    dw = detect_width
    dh = int(round(h * dw / w / 2)) * 2
    scale_x, scale_y = w / dw, h / dh

    args = [_tool("ffmpeg"), "-v", "error"]
    if start:
        args += ["-ss", f"{start:.3f}"]
    if end is not None:
        args += ["-to", f"{end:.3f}"]
    args += [
        "-i", video,
        "-vf", f"scale={dw}:{dh}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]

    det = _detector()
    wanted = set(classes)
    frame_bytes = dw * dh * 3
    out: list[FrameDetections] = []

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        idx = 0
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(dh, dw, 3)

            boxes: list[Box] = []
            try:
                # NudeNet accepts an ndarray directly (see nudenet._read_image).
                for r in det.detect(frame):
                    cls = r.get("class", "")
                    score = float(r.get("score", 0.0))
                    if cls not in wanted or score < min_score:
                        continue
                    bx, by, bw, bh = r["box"]
                    boxes.append(Box(
                        x=int(bx * scale_x), y=int(by * scale_y),
                        w=int(bw * scale_x), h=int(bh * scale_y),
                        cls=cls, score=score,
                    ))
            except Exception:  # noqa: BLE001 - one bad frame must not abort the pass
                pass

            out.append(FrameDetections(index=idx, time=start + idx / fps, boxes=boxes))
            if progress and idx % 50 == 0:
                progress(idx)
            idx += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()

    return out, w, h, fps


def _probe_size(video: str) -> tuple[int, int]:
    out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", video],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)


def smooth(
    frames: list[FrameDetections],
    hold: int = 6,
    dilate: float = 1.35,
    max_w: int = 10 ** 6,
    max_h: int = 10 ** 6,
) -> list[list[Box]]:
    """Carry boxes across detection dropouts, and dilate them.

    A box that disappears for a frame or two mid-scene is nearly always a detector miss,
    not the content leaving frame — and an unblurred flash is exactly the failure the user
    would notice. Each box is held for `hold` frames after its last sighting.

    `hold` also covers the reverse case at a scene's start only partially: the first
    detection is still the first blurred frame. `lead` in `plan_blur` handles that.
    """
    active: list[tuple[Box, int]] = []  # (box, frames remaining)
    out: list[list[Box]] = []

    for fd in frames:
        if fd.boxes:
            active = [(b, hold) for b in fd.boxes]
        else:
            active = [(b, n - 1) for b, n in active if n - 1 > 0]
        out.append([b.dilate(dilate, max_w, max_h) for b, _ in active])
    return out


def plan_blur(
    per_frame: list[list[Box]],
    lead: int = 4,
) -> list[list[Box]]:
    """Extend each blur run backwards by `lead` frames.

    `smooth` only holds boxes *forward*, so the frames just before the model first fires
    stay sharp — and that is where the content is already partly visible. Walking runs
    backwards closes that gap.
    """
    out = [list(b) for b in per_frame]
    n = len(out)
    for i in range(n):
        if not out[i]:
            continue
        # Only seed backwards at the start of a run.
        if i > 0 and out[i - 1]:
            continue
        for j in range(max(0, i - lead), i):
            out[j] = list(out[i])
    return out


def render_blurred(
    video: str,
    per_frame: list[list[Box]],
    dest: str,
    fps: float,
    width: int,
    height: int,
    strength: float = 0.25,
    pixelate: bool = True,
    crf: int = 18,
    progress=None,
) -> str:
    """Composite blurred regions frame by frame and encode to `dest`.

    Audio is copied from the source untouched. Video is re-encoded — unavoidable, since
    every pixel of the blurred region changes — at visually-lossless CRF.

    `strength` is the blur kernel as a fraction of the region's smaller side, so a small
    region gets proportionally as much destruction as a large one. Gaussian blur is the
    default; `pixelate` swaps in a mosaic, which is more obviously deliberate.
    """
    import cv2
    import numpy as np

    read_args = [
        _tool("ffmpeg"), "-v", "error", "-i", video,
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    write_args = [
        _tool("ffmpeg"), "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", f"{fps}",
        "-i", "-",
        "-i", video,
        "-map", "0:v:0", "-map", "1:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-shortest",
        dest,
    ]

    frame_bytes = width * height * 3
    src = subprocess.Popen(read_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dst = subprocess.Popen(write_args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    blurred_frames = 0
    try:
        idx = 0
        while True:
            raw = src.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(height, width, 3).copy()

            boxes = per_frame[idx] if idx < len(per_frame) else []
            if boxes:
                blurred_frames += 1
            for b in boxes:
                x0, y0 = max(0, b.x), max(0, b.y)
                x1, y1 = min(width, b.x + b.w), min(height, b.y + b.h)
                if x1 <= x0 or y1 <= y0:
                    continue
                roi = frame[y0:y1, x0:x1]
                frame[y0:y1, x0:x1] = (
                    _pixelate(roi, strength) if pixelate else _gaussian(roi, strength)
                )

            dst.stdin.write(frame.tobytes())
            if progress and idx % 50 == 0:
                progress(idx)
            idx += 1
    finally:
        if src.stdout:
            src.stdout.close()
        src.wait()
        if dst.stdin:
            dst.stdin.close()
        err = dst.stderr.read().decode(errors="replace") if dst.stderr else ""
        rc = dst.wait()
        if rc != 0:
            raise RuntimeError(f"encode failed ({rc}): {err[:800]}")

    return dest


def _gaussian(roi, strength: float):
    """Blur hard enough to destroy the region, not merely soften it.

    The kernel is a fraction of the region's own smaller side, so small boxes get
    proportionally the same destruction as large ones. A fixed floor (say 9px) is a trap:
    on an 80px region it is cosmetic and the content stays legible. The floor here scales
    with the region and sigma is driven from the kernel so the blur actually saturates.
    """
    import cv2

    h, w = roi.shape[:2]
    base = min(h, w)
    k = max(7, int(base * strength) | 1)
    # A Gaussian whose sigma is small relative to its kernel barely mixes pixels; tie
    # sigma to the kernel so the region flattens to its mean.
    sigma = k / 3.0
    return cv2.GaussianBlur(roi, (k, k), sigma)


def _pixelate(roi, strength: float):
    """Mosaic the region down to a handful of blocks across.

    `strength` here means "fraction of the region per block", so 0.25 gives ~4 blocks
    across regardless of resolution — coarse enough that no detail survives the
    downscale, which is the only thing that genuinely removes information.
    """
    import cv2

    h, w = roi.shape[:2]
    blocks = max(2, int(1 / max(strength, 0.01)))
    small = cv2.resize(roi, (blocks, blocks), interpolation=cv2.INTER_AREA)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)


def blur_video(
    video: str,
    dest: str,
    classes: tuple[str, ...] = DEFAULT_CLASSES,
    min_score: float = MIN_SCORE,
    hold: int = 6,
    lead: int = 4,
    dilate: float = 1.8,
    strength: float = 0.25,
    pixelate: bool = True,
    detect_width: int = 640,
    progress=None,
) -> dict:
    """Detect nudity across `video` and write a copy with those regions blurred.

    Returns a summary dict: frame counts, per-class totals, and the blurred time ranges.
    """
    frames, w, h, fps = detect_all_frames(
        video, classes=classes, min_score=min_score,
        detect_width=detect_width, progress=progress,
    )
    raw_hits = sum(1 for f in frames if f.boxes)
    held = smooth(frames, hold=hold, dilate=dilate, max_w=w, max_h=h)
    planned = plan_blur(held, lead=lead)

    render_blurred(video, planned, dest, fps=fps, width=w, height=h,
                   strength=strength, pixelate=pixelate)

    from collections import Counter

    per_class = Counter(b.cls for f in frames for b in f.boxes)
    return {
        "total_frames": len(frames),
        "frames_detected": raw_hits,
        "frames_blurred": sum(1 for b in planned if b),
        "per_class": dict(per_class),
        "ranges": _runs_to_ranges(planned, fps),
        "fps": fps,
        "size": [w, h],
        "output": dest,
    }


def _runs_to_ranges(per_frame: list[list[Box]], fps: float) -> list[list[float]]:
    ranges, start = [], None
    for i, boxes in enumerate(per_frame):
        if boxes and start is None:
            start = i
        elif not boxes and start is not None:
            ranges.append([round(start / fps, 2), round(i / fps, 2)])
            start = None
    if start is not None:
        ranges.append([round(start / fps, 2), round(len(per_frame) / fps, 2)])
    return ranges
