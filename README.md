# Movie Filter

Mutes profanity and cuts flagged scenes from your own media files, precisely, and proves
it worked.

Built for a Plex library on Unraid. Runs as a Docker container with a web UI.

## Why it exists

VidAngel's filter data gives approximate timings — `start_approx` values are quantised to
**6-second buckets**, and the word can sit anywhere from −0.7 s to +3.0 s from its bucket.
Applying those timings directly clips half a syllable, or misses the word entirely.

This tool treats the bucket as nothing more than a *search hint*. It finds the word in
your own audio with Whisper, refines the boundaries against audio energy, snaps them to
frame boundaries, then **renders the mute and re-checks that the word is actually gone**.

It also scans the whole track against your own word list, which routinely finds words no
tag-set covers.

## What it does

- **Library browser** over your media shares, showing filter status and whether a
  VidAngel tag-set is available for each title.
- **Word-accurate audio mutes** — located, verified, and iteratively tightened until the
  target is inaudible and neighbouring dialogue survives.
- **Full-track discovery scan** against an editable word list. Hits nothing covers are
  held for review, never muted silently.
- **Interactive review** — hear each hit in context, then mute or skip. Decisions are
  remembered.
- **Video cuts** snapped to real shot boundaries via scene detection.
- **Original always archived** before anything is written.
- **A report per run**: every incident, its drift from the bucket, verification status,
  and what needs your attention.

## Audio quality

Muting requires decoding and re-encoding — `-c:a copy` cannot apply a filter. Three
modes, per run:

| mode | what it does | cost |
|---|---|---|
| **splice** (default) | Re-encodes **only the frames overlapping a mute**; everything else is copied byte-for-byte | size-neutral (+600 KB on a 1.45 GB episode) |
| **same** | Re-encodes the whole track to the source codec | size-neutral, ~166 dB PSNR (inaudible) |
| **lossless** | FLAC | bit-exact, ~1.84× the audio size |

`splice` requires a same-codec encoder for the muted frames, so **DTS-HD MA, DTS:X and
TrueHD/Atmos automatically fall back to FLAC** — ffmpeg's `dca` encoder only emits the
lossy DTS core, and one audio stream cannot change codec mid-file.

**Atmos and DTS:X object metadata cannot survive filtering at all.** Muting needs PCM,
and nothing can rebuild the objects. For those titles the choice is a filtered 7.1 track
or an unfiltered one with Atmos.

## Running it

```bash
docker compose up -d          # then open http://<host>:8080
```

Or directly:

```bash
docker run -d --name movie-filter --gpus all -p 8080:8000 \
  -v /mnt/user/media:/media \
  -v /mnt/user/appdata/movie-filter:/data \
  movie-filter
```

GPU is optional — Whisper falls back to CPU at roughly 10–20× slower. Check
`/api/health` to see which device it actually got.

## VidAngel tag-sets

The server cannot fetch them: the API is tied to your logged-in account. Open
`https://api.vidangel.com/api/bff/tag-sets/<id>/` in your browser and paste the JSON into
the **Tag-sets** tab. It is cached and matched to titles by name.

Tag-sets are **optional**. They contribute category taxonomy (`profanity` vs `blasphemy`
vs `language_sexual`) and video-scene locations, which the word list cannot express. For
audio words alone, the word-list scan is more complete.

## Local development

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --reload
```

The command-line pipeline works standalone too:

```powershell
.venv\Scripts\python tools\run_filter.py <video> <tagset.json> `
    --quality splice --out filtered.mkv --archive original.mkv
```

## Notes

- `STATUS.md` — current state, verified results, and hard-won lessons worth not
  relearning.
- `DESIGN.md` — measurements behind the design decisions.
- `CLAUDE.md` — repo map and the constraints that are easy to break when changing code.
