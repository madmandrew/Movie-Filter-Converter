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
    """Put the pip-installed NVIDIA libraries on the loader's search path.

    `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` drop their libraries inside
    site-packages, where the loader won't find them. Without this, the model *loads*
    on CUDA but dies at inference with "cublas64_12.dll is not found" — so a
    load-only GPU probe is a false positive.

    The two platforms need different work: Windows keeps DLLs in `bin/` and wants
    `add_dll_directory`, Linux keeps .so files in `lib/` and reads LD_LIBRARY_PATH.
    Doing only the Windows half means the container silently runs on CPU.

    LD_LIBRARY_PATH is read by the dynamic linker at process start, so setting it
    here only helps libraries that have not been loaded yet. The Dockerfile exports
    it too, which is what actually covers the container.
    """
    import site

    roots = list(site.getsitepackages())
    if hasattr(site, "getusersitepackages"):
        roots.append(site.getusersitepackages())

    lib_dirs = []
    for root in roots:
        nvidia = os.path.join(root, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        for pkg in sorted(os.listdir(nvidia)):
            for sub in ("bin", "lib"):
                d = os.path.join(nvidia, pkg, sub)
                if os.path.isdir(d):
                    lib_dirs.append(d)

    for d in lib_dirs:
        if hasattr(os, "add_dll_directory"):  # Windows only
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

    if lib_dirs and not hasattr(os, "add_dll_directory"):
        prev = os.environ.get("LD_LIBRARY_PATH", "")
        merged = [d for d in lib_dirs if d not in prev.split(os.pathsep)]
        if merged:
            os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
                merged + ([prev] if prev else [])
            )


def _cuda_compute_types() -> list[str]:
    """Compute types to try on the GPU, best first.

    float16 is not universal: Pascal (GTX 1070/1080, compute 6.1) runs it at a
    fraction of full rate, and int8_float16 needs compute 7.0+. Hardcoding float16
    is what made those cards fall back to CPU. ctranslate2 knows what the device
    supports, so ask it and keep only the types it reports, best first.
    """
    order = ["float16", "int8_float16", "int8", "int8_float32", "float32"]
    try:
        import ctranslate2

        supported = set(ctranslate2.get_supported_compute_types("cuda"))
        ranked = [c for c in order if c in supported]
        if ranked:
            # Pascal reports float16 as supported but runs it at low rate, so prefer
            # int8 when the fast path is absent.
            if "int8_float16" not in supported and "int8" in supported:
                ranked = ["int8"] + [c for c in ranked if c != "int8"]
            return ranked
    except Exception:  # noqa: BLE001 - no CUDA, old ctranslate2, etc.
        pass
    return order


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
        _register_cuda_dlls()
        import numpy as np

        for ct in _cuda_compute_types():
            try:
                m = _model(size, "cuda", ct)
                list(m.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])
                print(f"[align] GPU ready (compute_type={ct})", flush=True)
                return m
            except Exception as exc:  # noqa: BLE001 - try the next type, then CPU
                print(
                    f"[align] compute_type={ct} unusable ({type(exc).__name__}: {exc})",
                    flush=True,
                )
                _model.cache_clear()
        print("[align] GPU unavailable, using CPU", flush=True)
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
