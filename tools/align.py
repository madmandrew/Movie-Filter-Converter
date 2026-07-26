"""
Locate the exact sub-second boundaries of a word inside a video, given only an
approximate timestamp.

VidAngel's tag-set API returns `start_approx` / `end_approx` — integer-second
estimates. At 23.976 fps a 1s error is ~24 frames, enough for a full syllable to
escape a mute. This module widens the estimate into a search window, transcribes
just that window with word-level timestamps, and returns the real boundaries.

Nothing here mutates the source file; audio is extracted to temp WAVs.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache

# winget put ffmpeg on PATH but fresh shells may not see it; fall back to the
# known install location before giving up.
_FFMPEG_FALLBACK = os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1.2-full_build\bin"
)


def _tool(name: str) -> str:
    from shutil import which

    found = which(name)
    if found:
        return found
    candidate = os.path.join(_FFMPEG_FALLBACK, f"{name}.exe")
    if os.path.exists(candidate):
        return candidate
    raise RuntimeError(f"{name} not found on PATH or at {_FFMPEG_FALLBACK}")


def probe_fps(video: str) -> float:
    """Real frame rate as a float. 24000/1001 -> 23.976023976..."""
    out = subprocess.run(
        [
            _tool("ffprobe"), "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "--", video,
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if "/" in out:
        num, den = out.split("/")
        return float(num) / float(den)
    return float(out)


def probe_duration(video: str) -> float:
    out = subprocess.run(
        [
            _tool("ffprobe"), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "--", video,
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def extract_audio(video: str, start: float, end: float, dest: str | None = None) -> str:
    """Extract [start, end] as 16 kHz mono WAV — what Whisper wants.

    `-ss` before `-i` seeks by keyframe (fast) but can land early; we accept that
    because the caller pads the window and we translate times back via `start`.
    """
    if dest is None:
        fd, dest = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
    start = max(0.0, start)
    subprocess.run(
        [
            _tool("ffmpeg"), "-v", "error", "-y",
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-i", video,
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            dest,
        ],
        check=True, capture_output=True,
    )
    return dest


def _register_cuda_dlls() -> None:
    """Put the pip-installed NVIDIA DLLs on the DLL search path.

    `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` drop their DLLs inside site-packages,
    where Windows won't find them. Without this, the model *loads* on CUDA but dies
    at inference with "cublas64_12.dll is not found" — so a load-only GPU probe is a
    false positive.
    """
    import site

    for root in site.getsitepackages():
        nvidia = os.path.join(root, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        for pkg in os.listdir(nvidia):
            bin_dir = os.path.join(nvidia, pkg, "bin")
            if os.path.isdir(bin_dir):
                try:
                    os.add_dll_directory(bin_dir)
                except (OSError, AttributeError):
                    pass
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


@lru_cache(maxsize=2)
def _model(size: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    return WhisperModel(size, device=device, compute_type=compute_type)


def get_model(size: str = "small.en", prefer_gpu: bool = True):
    """Load once and reuse — model init is far slower than a 4s transcription.

    Verifies the GPU by running a real inference, not just a load: cuBLAS/cuDNN
    failures only surface once the math kernels are touched.
    """
    if prefer_gpu:
        try:
            _register_cuda_dlls()
            m = _model(size, "cuda", "float16")
            import numpy as np

            list(m.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])
            return m
        except Exception as exc:  # noqa: BLE001 - any CUDA failure means fall back
            print(f"[align] GPU unavailable ({type(exc).__name__}), using CPU", flush=True)
            _model.cache_clear()
    return _model(size, "cpu", "int8")


@dataclass
class Word:
    text: str
    start: float          # absolute, seconds into the video
    end: float
    probability: float

    @property
    def norm(self) -> str:
        return re.sub(r"[^a-z]", "", self.text.lower())


def transcribe_window(
    video: str,
    start: float,
    end: float,
    model=None,
    hotwords: str | None = None,
) -> list[Word]:
    """Word-level transcript of [start, end], timestamps rebased to absolute.

    `hotwords` biases decoding toward expected terms. Whisper is trained on
    sanitised text and will soften or drop profanity — the exact words we care
    about — so passing the expected word materially improves recall.
    """
    model = model or get_model()
    wav = extract_audio(video, start, end)
    try:
        segments, _ = model.transcribe(
            wav,
            word_timestamps=True,
            condition_on_previous_text=False,   # window is out of context by design
            hotwords=hotwords,
            vad_filter=False,                   # never skip audio; we need every word
            beam_size=5,
        )
        words: list[Word] = []
        for seg in segments:
            for w in (seg.words or []):
                words.append(
                    Word(
                        text=w.word.strip(),
                        start=start + w.start,
                        end=start + w.end,
                        probability=w.probability,
                    )
                )
        return words
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
