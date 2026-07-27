"""
Nudity detection for video, using NudeNet.

Two jobs, mirroring how audio works:

* **Discovery** — sample frames across a title, classify them, and merge positive runs
  into scene ranges the user can review. Advisory only.
* **Verification** — after cutting, re-scan the output and assert the flagged content is
  gone. This is the video analogue of the audio energy check, and the reason cutting is
  worth doing automatically at all.

Unlike the audio pipeline there is **no ground truth** here: a classifier's opinion is not
a fact, and it has no notion of narrative context. So detections are never auto-applied —
they are surfaced for a human decision. What *is* automatic is the after-the-fact check
that whatever you chose to cut is actually absent from the output.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field

from align import _tool

#: Classes that count as nudity worth cutting. NudeNet also emits benign classes
#: (FEET_EXPOSED, ARMPITS_EXPOSED, BELLY_EXPOSED, and every *_COVERED variant); acting on
#: those would cut every beach, gym, and swimming scene in the library.
DEFAULT_CLASSES = (
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
)

#: Classes deliberately excluded, kept explicit so the reasoning is not lost.
BENIGN_CLASSES = (
    "FEET_EXPOSED", "ARMPITS_EXPOSED", "BELLY_EXPOSED", "MALE_BREAST_EXPOSED",
    "FACE_FEMALE", "FACE_MALE",
    # Every *_COVERED class means clothed.
    "FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED",
    "ANUS_COVERED", "BELLY_COVERED", "FEET_COVERED", "ARMPITS_COVERED",
)

#: Minimum detection confidence. NudeNet is noisy at low scores; 0.35 keeps recall
#: reasonable while cutting obvious false alarms.
MIN_SCORE = 0.35


@dataclass
class Detection:
    time: float
    cls: str
    score: float
    box: list[int] = field(default_factory=list)


@dataclass
class NudityRange:
    start: float
    end: float
    detections: list[Detection]
    method: str = "detected"

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def peak(self) -> Detection | None:
        return max(self.detections, key=lambda d: d.score) if self.detections else None

    @property
    def classes(self) -> list[str]:
        return sorted({d.cls for d in self.detections})


def _detector():
    """Load NudeNet once — model init dominates per-frame cost."""
    global _DET
    try:
        return _DET
    except NameError:
        from nudenet import NudeDetector

        _DET = NudeDetector()
        return _DET


def extract_frames(
    video: str,
    fps: float,
    dest_dir: str,
    start: float = 0.0,
    end: float | None = None,
    width: int = 640,
) -> list[tuple[float, str]]:
    """Sample frames at `fps` into `dest_dir`. Returns (timestamp, path) pairs.

    Downscaling to `width` speeds detection considerably and does not hurt it — the model
    works on 320px input internally.
    """
    os.makedirs(dest_dir, exist_ok=True)
    pattern = os.path.join(dest_dir, "f_%06d.jpg")
    args = [_tool("ffmpeg"), "-v", "error", "-y"]
    if start:
        args += ["-ss", f"{start:.3f}"]
    if end is not None:
        args += ["-to", f"{end:.3f}"]
    args += ["-i", video, "-vf", f"fps={fps},scale={width}:-2",
             "-q:v", "4", pattern]
    subprocess.run(args, check=True, capture_output=True)

    out = []
    for name in sorted(os.listdir(dest_dir)):
        if not name.startswith("f_"):
            continue
        idx = int(name[2:8])
        # ffmpeg's fps filter emits frame n at n/fps into the trimmed input.
        out.append((start + (idx - 1) / fps, os.path.join(dest_dir, name)))
    return out


def detect_frames(
    frames: list[tuple[float, str]],
    classes: tuple[str, ...] = DEFAULT_CLASSES,
    min_score: float = MIN_SCORE,
    progress=None,
) -> list[Detection]:
    """Classify sampled frames, keeping only actionable classes above `min_score`."""
    det = _detector()
    wanted = set(classes)
    found: list[Detection] = []

    for i, (ts, path) in enumerate(frames):
        try:
            raw = det.detect(path)
        except Exception:  # noqa: BLE001 - a bad frame must not abort a long scan
            continue
        for r in raw:
            cls = r.get("class", "")
            score = float(r.get("score", 0.0))
            if cls in wanted and score >= min_score:
                found.append(Detection(time=ts, cls=cls, score=score,
                                       box=r.get("box") or []))
        if progress and i % 25 == 0:
            progress(i, len(frames))
    return found


def group_detections(
    detections: list[Detection],
    gap: float = 3.0,
    min_hits: int = 2,
) -> list[NudityRange]:
    """Merge nearby detections into ranges.

    `min_hits` rejects isolated single-frame detections, which are usually false
    positives — a real scene persists across several sampled frames. Raising it trades
    recall for precision.
    """
    if not detections:
        return []

    ordered = sorted(detections, key=lambda d: d.time)
    groups: list[list[Detection]] = [[ordered[0]]]
    for d in ordered[1:]:
        if d.time - groups[-1][-1].time <= gap:
            groups[-1].append(d)
        else:
            groups.append([d])

    out = []
    for g in groups:
        if len(g) < min_hits:
            continue
        out.append(NudityRange(start=g[0].time, end=g[-1].time, detections=g))
    return out


def scan_video(
    video: str,
    sample_fps: float = 1.0,
    classes: tuple[str, ...] = DEFAULT_CLASSES,
    min_score: float = MIN_SCORE,
    min_hits: int = 2,
    start: float = 0.0,
    end: float | None = None,
    progress=None,
) -> list[NudityRange]:
    """Full-title nudity scan. Returns candidate ranges for review, never for auto-cut."""
    tmp = tempfile.mkdtemp(prefix="nudescan_")
    try:
        frames = extract_frames(video, sample_fps, tmp, start=start, end=end)
        dets = detect_frames(frames, classes, min_score, progress=progress)
        return group_detections(dets, min_hits=min_hits)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def verify_absent(
    video: str,
    ranges: list[tuple[float, float]],
    classes: tuple[str, ...] = DEFAULT_CLASSES,
    sample_fps: float = 4.0,
    min_score: float = MIN_SCORE,
) -> list[Detection]:
    """Re-scan the given ranges of a rendered file and report anything still detected.

    Sampled denser than discovery (4 fps vs 1) because this is the check that matters:
    a single surviving frame is a failure, and the ranges are short.
    """
    survivors: list[Detection] = []
    for s, e in ranges:
        tmp = tempfile.mkdtemp(prefix="nudeverify_")
        try:
            frames = extract_frames(video, sample_fps, tmp,
                                    start=max(0.0, s - 0.5), end=e + 0.5)
            survivors.extend(detect_frames(frames, classes, min_score))
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
    return survivors


def snap_to_scenes(
    ranges: list[NudityRange],
    cuts: list[float],
    tolerance: float = 4.0,
    pad: float = 1.0,
    duration: float | None = None,
) -> list[NudityRange]:
    """Expand detected ranges out to enclosing shot boundaries.

    Detection is sampled (typically 1 fps), so a range's true extent is wider than its
    first and last hit. Snapping outward to real shot cuts removes the whole scene rather
    than a fragment of it — which is what the user wants for nudity, and also hides the
    sampling coarseness.
    """
    from scenes import _nearest

    out = []
    for r in ranges:
        before = [c for c in cuts if c <= r.start]
        after = [c for c in cuts if c >= r.end]
        s_cut = _nearest(before, r.start, tolerance) if before else None
        e_cut = _nearest(after, r.end, tolerance) if after else None

        start = s_cut if s_cut is not None else r.start - pad
        end = e_cut if e_cut is not None else r.end + pad
        method = f"{'cut' if s_cut is not None else 'pad'}/" \
                 f"{'cut' if e_cut is not None else 'pad'}"

        start = max(0.0, start)
        if duration is not None:
            end = min(duration, end)
        out.append(NudityRange(start=start, end=end, detections=r.detections,
                               method=method))
    return out
