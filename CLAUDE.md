# CLAUDE.md

> Read `README.md` for what the app does and how to run it, `STATUS.md` for where the work
> stands, and `DESIGN.md` for the intended shape of the build. This file is the map for
> working *in* the code.

## What this project is

A FastAPI web app that mutes profanity and cuts flagged scenes from the user's own media
files, for a Plex library on Unraid. Deployed as a Docker container with GPU passthrough.

The 2022 Create React App that used to live in `src/` was **deleted on 2026-07-27**. It
converted pasted filter JSON into VideoSkip/EDL text and was never part of this pipeline.
Its output formats live on in `tools/render.py` and the VideoSkip client. To read it:
`git show HEAD~1:src/components/FilterUtils.ts`.

## Repo layout

```
app/                 FastAPI web app
  main.py            HTTP API + page serving; RunIn is the run-options contract
  jobs.py            single-threaded run queue; _execute() is the whole pipeline
  db.py              SQLite schema and helpers
  library.py         media-root browsing, path containment
  autofetch.py       tag-set lookup by title
  vidangel_client.py VidAngel API (auth is `Token`, not Bearer)
  videoskip_client.py
  titleparse.py      release-filename -> title/year/season/episode
  static/app.js      the entire front end, one file, no framework
  templates/index.html
tools/               the filtering pipeline, importable and CLI-runnable
tests/               pytest; NOT installed in .venv, so they do not run locally as-is
testdata/            sample tag-sets
```

No router, no bundler, no build step for the front end — `app.js` is served as-is with an
mtime cache-buster.

## The pipeline (`tools/`)

| file | role |
|---|---|
| `align.py` | ffmpeg/ffprobe wrappers, audio extraction, Whisper word timestamps, CUDA DLL registration |
| `locate.py` | bucket -> exact word boundaries: matching, energy edge refinement, frame snapping |
| `verify.py` | under/over-mute checks, `tighten()` loop, mute rendering |
| `scan.py` | full-track discovery scan, dedup, cross-reference against planned mutes |
| `offset.py` | measures the source-to-file offset from the audio |
| `scenes.py` | shot-boundary detection, outward snapping of video ranges |
| `nudity.py` | NudeNet discovery scan + `verify_absent`; classes to act on vs benign |
| `cut.py` | standalone: excise nudity ranges and concat the remainder (CLI only) |
| `blur.py` | standalone: pixelate/blur detected regions (CLI only, not wired into the app) |
| `splice.py` | byte-exact audio splicing for the `splice` quality mode |
| `render.py` | produces the filtered file; `render()` takes mutes + video_cuts |
| `vidangel.py` | tag-set parsing, ref_id dedup, category allowlist |
| `run_filter.py`, `run_cut.py`, `run_blur.py` | CLI drivers |

`cut.py` / `blur.py` duplicate some of what the app path does through
`nudity.py -> video_ranges -> render.py`. They are kept because they run standalone
without the DB, but **the app does not import them** — changing them does not change app
behaviour.

## Things that are load-bearing and easy to break

1. **VidAngel timings are 6-second buckets.** Every `start_approx` is a multiple of 6 and
   the real word sits −0.7 s to +3.0 s away. Nothing may trust them as timestamps; they
   are search hints only.
2. **Verification is by audio energy, not transcript diffing.** Whisper hallucinates on
   silence and its decode varies run to run, so "the word is gone from the transcript" is
   not evidence. See `verify.py`.
3. **Cutting invalidates every later timestamp.** Ranges are resolved against the *source*
   timeline and applied in one pass. Mapping an output time back to source means walking
   the cuts in order against a running output position — comparing an output time directly
   against a source-time cut boundary mixes timelines. (`jobs.py: _to_source`)
4. **Nudity ranges under-report their extent.** Sampled detection reports a start later
   than the truth: a hit lands up to one sample interval late, and `min_hits` discards the
   leading hits. Measured: 2 fps / min_hits=2 reported 15.5 s for content starting at
   13.5 s. The pad is `(min_hits + 2) / fps`, not a constant.
5. **The archive is mandatory before any write.** Cuts are destructive and a second pass
   over an already-filtered file compounds the damage, so a re-run filters *from the
   archive*, not from the file on disk.
6. **CUDA DLLs.** `nvidia-cublas-cu12`/`nvidia-cudnn-cu12` install where Windows cannot
   find them; `align._register_cuda_dlls()` fixes it. A load-only GPU probe is a false
   positive — the model loads on CUDA and then dies at inference, so `get_model()` runs a
   real inference to check.
7. **Category matching is by key, not display string.** Upstream wording changes must not
   silently drop a category.

## Conventions

- ffmpeg/ffprobe are resolved through `align._tool()`, never bare `ffmpeg` — winget's
  PATH entry is missing in fresh shells and there is a known-location fallback.
- Long analysis functions take a `progress` callback rather than printing.
- A classifier's opinion is never auto-applied. Nudity and scan hits are surfaced for
  review; only the after-the-fact verification is automatic.
- Front-end helpers (`tc`, `parseTime`, `esc`, `toast`) already exist in `app.js` — reuse
  them rather than reimplementing.

## Commands

The **deployed instance is `http://192.168.50.31:8181`** (`docker-compose.deploy.yml`) —
not 8080, which qBittorrent owns on that server. `/api/health` reports the device Whisper
actually got; query it rather than inferring the device from run timings.

```
docker compose up -d                     # local dev (port 8080 -> 8000)
uvicorn app.main:app --reload            # local dev, needs PYTHONPATH=tools:app
.venv/Scripts/python.exe tools/run_filter.py <video> <tagset.json> --out out.mkv
.venv/Scripts/python.exe tools/run_cut.py  <video> <out.mp4>
```

`pytest` is **not** installed in `.venv`; `tests/` cannot be run without installing it
first. Do not claim the suite passes without having actually run it.
