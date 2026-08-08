# Status / handoff

Last updated **2026-07-27**. Read this first when picking the project back up.
See also `CLAUDE.md` (working in the code) and `DESIGN.md` (the intended future build).

---

## Where things stand

The **audio word-muting pipeline works end to end and is verified.** The web app wraps it
with a library browser, a run queue, and interactive review.

The 2022 React app that lived in `src/` was **deleted on 2026-07-27** — it was never part
of this pipeline. Recover it from git history if its output formats are ever needed:
`git show <commit-before-deletion>:src/components/FilterUtils.ts`.

### Verified result (Community S01E02, tag-set 46025)

| ref_id | word | VidAngel bucket | final mute | drift | status |
|---|---|---|---|---|---|
| 1948557 | hell | 312 | 311.269–311.520 | −0.73s | OK |
| 1947662 | ass | 576 | 578.536–578.953 | +2.54s | OK |
| 1947667 | hell | 594 | 595.804–596.304 | +1.80s | OK_VIA_SCAN |
| 1947684 | hell | 930 | 932.890–933.307 | +2.89s | OK |
| 1947685 | damn | 960 | 962.503–963.004 | +2.50s | OK (clipped `real`,`paper`) |

**5/5 located, 5/5 verified, 0 not found.** Plus one advisory finding: an untagged
`hell` at 15:34.320 that VidAngel missed entirely.

The user listened to the output and confirmed: **"the audio cuts were perfect."**

---

## The core insight

VidAngel's timings are **not** precise and never will be. `start_approx` is quantised
to **6-second buckets** (every value in the payload is a multiple of 6), and the real
word sits anywhere from −0.7s to +3.0s away from its bucket. That, not
millisecond rounding, is why filtering was landing half-on-the-word.

So the pipeline does **not** trust VidAngel's timing at all. It uses the bucket only as
a *search hint*, then finds the word itself in the local audio:

```
bucket ±7s  ->  Whisper word-level transcript  ->  match expected word
            ->  refine edges on audio energy   ->  snap to frame boundaries
            ->  render mute, verify, tighten until clean
```

---

## Toolchain

| thing | where |
|---|---|
| Python venv | `.venv/` (Python 3.12.10) |
| Whisper | `faster-whisper` 1.2.1, model `small.en`, **CUDA on the RTX 3050 Ti** |
| ffmpeg/ffprobe | 8.1.2, `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-8.1.2-full_build\bin` |
| test video | `C:\Users\andre\Videos\Community.S01E02.1080p.BluRay.x264-YELLOWBiRD.mkv` |
| test tag-set | `testdata/tagset-46025.json` |
| Unraid library | `\\192.168.50.31\media` mapped as `Z:` (read-only, user `smbuser`) |

**CUDA gotcha:** `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` install their DLLs inside
site-packages where Windows can't find them. `align._register_cuda_dlls()` fixes this.
Without it the model *loads* on CUDA but dies at inference — so a load-only GPU probe
is a false positive. `get_model()` now runs a real inference to check.

**Perf:** ~13x realtime on GPU. A 12s window transcribes in 0.9s; a full 21-min episode
scan takes ~67s.

---

## Code map (`tools/`)

| file | role |
|---|---|
| `align.py` | ffmpeg/ffprobe wrappers, audio extraction, Whisper word-level transcription, CUDA DLL registration |
| `locate.py` | bucket → exact boundaries: word matching, energy-based edge refinement, frame snapping |
| `verify.py` | under/over-mute checking, `tighten()` iteration loop, mute rendering |
| `scan.py` | full-episode discovery scan, hit dedup, cross-reference against planned mutes |
| `vidangel.py` | tag-set JSON parsing, ref_id dedup, category allowlist, runtime pre-flight |
| `run_filter.py` | CLI driver tying it all together; EDL, JSON report, lossless render |

### Run it

```powershell
.venv\Scripts\python.exe tools\run_filter.py `
    "C:\Users\andre\Videos\Community.S01E02.1080p.BluRay.x264-YELLOWBiRD.mkv" `
    "testdata\tagset-46025.json" `
    --json testdata\S01E02.report.json `
    --edl  testdata\S01E02.edl `
    --out  "C:\Users\andre\Videos\Community.S01E02.FILTERED.mkv" `
    --archive "C:\Users\andre\Videos\unfilteredArchive\Community.S01E02.ORIGINAL.mkv"
```

Flags: `--categories damn,hell,ass` (default), `--no-scan`, `--only-enabled`.

---

## Hard-won lessons — do not re-litigate these

### 1. Whisper hallucinates words from silence

On a **correctly muted** clip Whisper emitted `'mother'` (p=0.08) to complete
"...is her ___". Worse, passing the expected word as `hotwords` — which genuinely helps
*finding* it — made Whisper emit `'partner'` (p=0.04) **from silence**, i.e. the
verifier manufactured the exact failure it was testing for.

- Locating: `hotwords=<word>` **on** (improves recall).
- Verifying: `hotwords=None` **always**, plus a confidence floor.
- Real speech scores p>0.5; hallucinations score p<0.10. Threshold at 0.25.

### 2. Verification must be energy-based, not transcript-based

The same audio decodes differently run to run ("news" → `'noons'`; a word's confidence
swinging 0.84 → 0.22). Diffing a baseline transcript against a muted one produces
phantom "lost neighbour" failures. Every early FAIL was a **good mute misjudged by a bad
test**.

- **Under-mute check:** RMS energy inside the mute vs. loudest nearby 200ms of speech
  (≤6% ratio). Ground truth, immune to the language model guessing.
- **Over-mute check:** *geometric* — does the mute cover >35% of a neighbouring word in
  the baseline transcript? No second transcript, so no decode variance.

### 3. Lenient matching causes false mutes

Mapping `"as"` as a softened variant of `"ass"` produced 5 false hits per episode ("as
it unfolds", "as a group"). Short profanity hides inside ordinary words —
`assignment`, `class`, `hello`, `shell`. Guarded by `_EXACT_ONLY`, `_FALSE_FRIENDS`,
and `_NEVER` in `locate.py`. Full-episode hits went 12 → 6, all genuine.

### 4. Whisper's word spans are unreliable; energy refinement is essential

In "what the hell are you", Whisper gave `'the'` a **0 ms** span (932.120 → 932.120)
and `'hell'` **1040 ms** — it collapses one token and dumps the slack into its
neighbour. `locate.refine_edges()` converts that into the correct ~417 ms mute. **Never
display or mute raw Whisper spans.**

### 5. The full-episode scan is genuinely necessary

It is not redundant double-checking. On S01E02 it:
- **recovered a NOT_FOUND** — `hell` @594, missed by the narrow window *and* by a manual
  wide search, purely from decode variance. Now an automatic fallback
  (`RECOVER_WINDOW = 8.0`), taking recall 4/5 → 5/5.
- **found a word VidAngel never tagged** — "What the hell are you? What the hell are
  you?" is two utterances 1.4s apart and only one is in the tag-set.

Scan hits stay **advisory**. Reporting cannot over-mute; auto-muting could.

### 6. PowerShell `[ ]` in paths

Release dirs like `Community.S01...[rartv]` contain brackets, which PowerShell treats as
wildcards — `Get-ChildItem` silently returns nothing. **Use `-LiteralPath`.**

### 7. `net view` is useless for Unraid

Returns "System error 5" even when SMB is healthy (Windows disabled the NetBIOS browser
service). Probe a known share path directly. Also: Unraid's **`root` cannot authenticate
over SMB** — it isn't in Samba's password DB. Use a non-root user.

---

## Decisions made

- **Mute individual swear words, not phrases or sentences.** Rules out VidAngel's
  `other_*` prose-described tags, which name no word.
- **Test scope: `damn`, `hell`, `ass`.** (`DEFAULT_CATEGORIES` in `vidangel.py`.)
- **Contiguous words: option (a) — remove the swear word, accept clipping the
  neighbour**, and report it. In "a real damn paper" the three words share boundaries
  with zero silence, so no clean word-only mute exists.
- **Output: single filtered file, original archived separately.** Rejected dual-track
  MKV (risk of selecting the wrong track) and EDL-as-default (mutes must be baked in).
  Video stream is copied, never re-encoded. Archive is mandatory because video cuts are
  destructive.
- **Audio quality is a per-run choice** (`--quality same|lossless`), to be a per-title
  toggle in the UI. TV → `same` (source codec, size-neutral, inaudible loss);
  treasured movies → `lossless` (FLAC). See the codec table below — `same` is
  automatically refused for lossless/object sources.
- **VidAngel is no longer needed for audio word filtering** — a word list + full scan
  strictly dominates the tag-set. Keep tag-sets as optional enrichment for category
  taxonomy and sanity-check counts. Don't block a run on VidAngel having the title.

## Measured facts worth keeping

- **Audio sizes** (60s of the DTS 5.1 1536 kbps source): copy 11.01 MB · FLAC 20.29 MB
  · FLAC level 12 20.26 MB · TrueHD 22.18 MB · EAC3 640k 4.58 MB · **DTS re-encode
  10.99 MB**. FLAC is ~1.84x; compression level is not worth tuning.
- **Splice mode is implemented and is now the default** (`--quality splice`,
  `tools/splice.py`). It re-encodes only the frames overlapping a mute and copies the
  rest **byte-for-byte**. Verified on the real episode: **0.14% of bytes re-encoded, all
  copied regions byte-identical** (direct byte comparison of the raw stream, not PSNR),
  DTS 5.1 preserved, 1.4492 GB vs 1.4486 GB source, all mutes silent (RMS 0.0) and
  unfiltered speech unchanged (399.8 → 399.4).

  Getting there required four fixes, each found by measurement — **do not undo these**:
  1. **`-ss` must come AFTER `-i`** (output seek). With input seek + `-c:a copy` ffmpeg
     starts at a packet boundary and *includes* preceding packets: **+2016 ms excess on
     one segment**, compounding to 18.5 s of drift.
  2. **Splice by byte offset on the raw elementary stream, not by timestamp on a
     container.** Per-segment container trims each round independently (~1 ms each),
     which accumulated to 24 ms and slid the last mute off its target (RMS 60.9 instead
     of 0). DTS has fixed 2012-byte/512-sample frames and the 241 MB stream divides with
     **zero remainder**, so byte cuts are exact by construction.
  3. **Pad the muted range by `EDGE_FRAMES = 2`.** Replacing only the exactly-overlapping
     frames still left audible decoder ringing at the seams (~40 RMS vs 830 in source —
     95% down but not silent), because lossy transform codecs overlap adjacent frames.
  4. **Cannot work for DTS-HD MA / DTS:X / TrueHD** — the muted segment has no same-codec
     encoder and one audio stream cannot change codec mid-file. `can_splice()` detects
     this and `render()` falls back to `lossless` (FLAC) automatically.
- **Measured quality (`apsnr` vs. the original):** FLAC = **inf dB** (bit-exact).
  DTS re-encode via `dca` = **161–172 dB across all 6 channels** — below the ~144 dB
  noise floor of 24-bit audio, i.e. inaudible. So re-encoding plain DTS to DTS is
  size-neutral *and* transparent; my initial "never round-trip through `dca`" was too
  cautious for plain DTS.
- **BUT the library is mostly lossless/object audio** — DTS-HD MA, DTS:X, TrueHD+Atmos
  (see `DESIGN.md` table). ffmpeg has **no usable encoder** for those: `dca` emits only
  the lossy core and the `truehd` encoder can't carry Atmos. `audio_args()` therefore
  refuses `same` for them and forces FLAC with a printed warning.
- **Atmos/DTS:X object metadata cannot survive filtering at all** — decode to PCM is
  mandatory and nothing can reconstruct the objects. Genuine limitation; disclose it.
- **Real filtered episode:** `same` → 1.453 GB vs 1.449 GB source (+4 MB, flat).
  `lossless` → 1.652 GB (+14%).
- **Scene-cut detection works and 6 of 10 VidAngel video buckets land within ~1.2s of a
  real shot boundary** (four within 350ms). Snapping video cuts to detected cuts is the
  video analogue of energy refinement. Command and caveats in `DESIGN.md`.
- **`runtime_unaltered` vs. ffprobe duration** is a free pre-flight wrong-cut check
  (46025: 1282 vs 1278.45 = same cut).

## Web app

`app/` is a FastAPI service (see `README.md` to run it). Verified against the real library:
**2,253 titles scanned in 37 s over SMB** (1,883 TV, 333 movies, 34 toFilter), and a full
filter run — 5 audio incidents, 3 video cuts, archive, render — in **4m42s**.

- `app/db.py` — SQLite: titles, runs, wordlist, review decisions, cached tag-sets
- `app/library.py` — library scan (lazy ffprobe), search, archive-path templating,
  `within_roots()` path guard
- `app/jobs.py` — single-threaded job queue; one GPU job at a time
- `app/main.py` — API + basic auth middleware
- `app/static/`, `app/templates/` — the UI

**Auth**: off unless `FILTER_PASSWORD` is set. **Set it whenever the app is reachable
beyond the LAN** — it can read arbitrary paths and overwrite media. `FILTER_USER` defaults
to `admin`.

**Archive destination** is a template (Settings tab), default
`{root}/toFilter/unfilteredArchive/{name}`. `{root}` resolves to the volume holding the
most library roots — a plain `commonpath` over roots on two drives picked the wrong volume
and would have archived to the system disk.

**Manual filtering** needs no tag-set: a word + rough time (located and verified
precisely), an explicit mute range, or a video cut with optional scene snapping.

## Deployment notes

- **Hardware video encoders are probed by running a real encode.** Being listed in
  `ffmpeg -encoders` means nothing: on the dev laptop `h264_nvenc` is listed but fails
  (driver nvenc API 13.0 vs. required 13.1), while `h264_qsv` works at **6.62x realtime**.
  The deployment target is Unraid with a **GTX 1070/1080** — NVENC is tried first there and
  QSV is absent. Do not re-tune this order for whichever machine is in front of you.
- Software fallback is `libx264 -preset veryfast -crf 18` (2.35x realtime). Anything
  slower than `medium` is unusable: `slow` measured **0.47x realtime**, ~45 min/episode.
- **Every audio track is filtered.** Real files have many: Joker 7, Lord of War 4,
  Apocalypse Now 2. Mapping only `a:0` would leave a selectable *unfiltered* track. Codec
  options are per-stream, or a single `-c:a` converts an AC3 commentary track to DTS.
- Browsers cache `app.css`/`app.js` hard; asset URLs carry an mtime query string. A CSS
  `display` rule at equal specificity *after* `.hidden` will win — `.hidden` is
  `!important` for exactly that reason.

## Next session — requested by the user (2026-07-27)

1. **Auto-pull VidAngel filters.** The pieces exist and are verified (search → resolve →
   fetch, see `app/vidangel_client.py`); what is missing is doing it *without being asked*
   — on library scan, or when a title is opened, match it to a work and fetch the
   matching tag-set automatically. Needs a title-matching heuristic better than the
   current `_looks_like`, and a decision about which offering to fetch when several exist
   (probably all of them: the offset estimator makes the choice unimportant, and runtimes
   let the UI show which is closest).
2. **Link to videoskip.com** from the UI, for manually grabbing a filter when VidAngel has
   nothing. The parser and upload path already work; this is a convenience link plus
   guidance, ideally pre-filled with the title being filtered.
3. **Tailscale.** Blocked on the user: the winget install stalls on a UAC prompt this
   session cannot click, and login is interactive. Once installed, rebind the server from
   `127.0.0.1` to `0.0.0.0` and keep basic auth on. Also wants it on the Unraid server.
   The Cloudflare quick tunnel is the stopgap and it dies every few hours.
4. **Filter popup adjustments** — user has specific changes in mind, unspecified. Ask
   before redesigning.

## Not done / next up

- **The Docker image has never been built or run.** GPU passthrough on Unraid is
  unverified; `/api/health` reports which device Whisper actually got.
- **Only ever run on one episode** (Community S01E02). Behaviour across titles with
  different audio characteristics is unmeasured.
- **Wrong-cut sources are handled** — `tools/offset.py` estimates the source-to-file offset
  from the scan and was verified exact to 0.0000s up to +250s, with a −45s shifted tag-set
  locating 5/5 through the full pipeline. The old `offsets.txt` notes were stale guesses
  and are no longer treated as data; the measurement comes from the files.
- **…but the audio estimate can fail in a way no re-run fixes**, and then tagged video cuts
  are refused. Severance S02E04 (prod run 61): drift was *bimodal* — one cluster near −90s,
  another at +21..+95s — so `estimate()` reported "no reliable offset (5/18 tags agreed)",
  the guard in `jobs.py` discarded five selected cuts, and the run finished having cut
  nothing while reporting success. **Fix: anchor the offset on a credits marker by hand**
  (Timeline offset in the filter dialog). Structural markers are hard boundaries the user
  can read off the file, so they drift far less than a 6s-bucketed word tag. A manual
  offset overrides the estimator *and* counts as a verified timeline, so cuts are applied.
  Verified against the real tag-set: closing credits tagged 3054s (50:54), actually at
  49:21 → −93s, and all five ranges then resolve. Two anchors disagreeing by >5s is
  surfaced as a warning — that is the signal drift is not constant and no single offset
  fits.
- **A read-only media mount blocks runs.** The default archive path resolves under the
  media root, so filtering anything on the SMB share needs either a writable archive
  location in Settings or a writable share. The run fails loudly rather than producing a
  filtered file with no archive.
- **`model="small.en"` is unvalidated as a choice** — `medium.en` may improve recall on
  the harder cases and fits in 4 GB VRAM. Untested.
- **Test coverage is one file** (`tests/test_matcher.py`, 62 cases). The pipeline,
  splice, and API have no automated tests; validation has been manual scripts.
- **The default word list is broad** (19 words incl. `god`, `douche`), so scans surface
  more for review than the damn/hell/ass test scope did. Prune in the Word list tab.
- **Interactive review applies on the *next* run.** Decisions are stored, not applied
  retroactively — a re-run is needed to act on them.
- **Duplicate titles are not detected.** The Godfather appears twice in the library
  (24.92 GB and 87.46 GB, presumably one already filtered by hand) and the UI cannot tell
  them apart.
- **A running job cannot be cancelled**, only a queued one — ffmpeg/Whisper are mid-write
  and killing them risks a partial file beside a real library file.
