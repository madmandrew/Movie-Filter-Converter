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
import threading
from dataclasses import dataclass, field
from functools import lru_cache


class Cancelled(Exception):
    """Raised when the user cancelled the run this thread is executing.

    Distinct from a failure: the caller catches it to mark the run 'cancelled' rather
    than 'failed', and to skip the error reporting a real crash gets.
    """


#: Per-thread cancellation state. The worker runs one job at a time on its own thread,
#: and the flag has to be readable from the *request* thread that sets it, so it is keyed
#: on the thread doing the work rather than passed down through every function signature —
#: the pipeline is a dozen modules deep and threading a token through all of them would
#: touch every call site in `tools/`.
_CANCEL: dict[int, threading.Event] = {}
_CHILDREN: dict[int, set] = {}
_LOCK = threading.Lock()


def arm_cancel(thread_id: int | None = None) -> threading.Event:
    """Start tracking cancellation for a thread. Returns the event that requests it."""
    tid = thread_id if thread_id is not None else threading.get_ident()
    with _LOCK:
        ev = _CANCEL.get(tid)
        if ev is None:
            ev = threading.Event()
            _CANCEL[tid] = ev
        _CHILDREN.setdefault(tid, set())
    return ev


def disarm_cancel(thread_id: int | None = None) -> None:
    tid = thread_id if thread_id is not None else threading.get_ident()
    with _LOCK:
        _CANCEL.pop(tid, None)
        _CHILDREN.pop(tid, None)


def request_cancel(thread_id: int) -> bool:
    """Ask the thread to stop, and kill whatever it is currently running.

    Killing the child is the part that matters: every ffmpeg/ffprobe call in this
    pipeline is a blocking `subprocess.run`, so a cooperative flag alone would not be
    noticed until the current one returned — up to tens of minutes into a render.
    """
    with _LOCK:
        ev = _CANCEL.get(thread_id)
        if ev is None:
            return False
        ev.set()
        children = list(_CHILDREN.get(thread_id, ()))
    for proc in children:
        try:
            proc.kill()
        except Exception:
            # Already exited between the snapshot and the kill; nothing to do.
            pass
    return True


def cancelled(thread_id: int | None = None) -> bool:
    tid = thread_id if thread_id is not None else threading.get_ident()
    with _LOCK:
        ev = _CANCEL.get(tid)
    return bool(ev and ev.is_set())


def check_cancelled() -> None:
    """Raise if this thread's run has been cancelled. Call between pipeline stages."""
    if cancelled():
        raise Cancelled("cancelled by the user")


def register_child(proc) -> None:
    """Track a process this thread started, so `request_cancel` can kill it.

    For callers that need the handle themselves — streaming a child's output rather than
    waiting on it — and so cannot go through `run_proc`.
    """
    with _LOCK:
        _CHILDREN.setdefault(threading.get_ident(), set()).add(proc)


def unregister_child(proc) -> None:
    with _LOCK:
        _CHILDREN.get(threading.get_ident(), set()).discard(proc)


def run_proc(args: list[str], **kw):
    """`subprocess.run`, but the child is killable by `request_cancel`.

    Every ffmpeg/ffprobe invocation in the pipeline goes through here so that a cancel
    can reach the process actually holding the run up.
    """
    check_cancelled()
    tid = threading.get_ident()
    # Popen rather than subprocess.run: the handle has to be registered before the wait
    # begins, or a cancel arriving during a long encode finds nothing to kill.
    capture = kw.pop("capture_output", False)
    check = kw.pop("check", False)
    timeout = kw.pop("timeout", None)
    if capture:
        kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE)
    proc = subprocess.Popen(args, **kw)
    with _LOCK:
        _CHILDREN.setdefault(tid, set()).add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        with _LOCK:
            _CHILDREN.get(tid, set()).discard(proc)
    # A killed child looks like an ordinary non-zero exit, so distinguish the two before
    # `check` turns it into a CalledProcessError the caller would report as a crash.
    if cancelled():
        raise Cancelled("cancelled by the user")
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, out, err)
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


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
    out = run_proc(
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
    out = run_proc(
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
    run_proc(
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
