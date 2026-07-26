# Status / handoff

Last updated **2026-07-25**. Read this first when picking the project back up.
See also `CLAUDE.md` (the legacy 2022 app) and `DESIGN.md` (the intended future build).

---

## Where things stand

The **audio word-muting pipeline works end to end and is verified.** The old 2022 React
app in `src/` is untouched and is not part of this work — it is legacy reference only.

Nothing has been committed. `git status` is dirty with all the new work.

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

## Not done / next up

- **Nothing is committed.** Consider a branch + commit before the next round of work.
- **Video cuts are not implemented at all** — only audio muting. Scene-snapping is
  researched (see `DESIGN.md`) but unwritten.
- **No automated tests.** The old `src/App.test.tsx` is the stock CRA test and fails.
  The `tools/` code has none; all validation so far has been manual scripts.
- **Only ever run on one episode.** The +2.5s drift pattern may not hold across titles,
  and the wrong-master cases from `offsets.txt` (godfather +12s, 8 mile +11s) have not
  been tested against this pipeline at all.
- **`model="small.en"` is unvalidated as a choice** — `medium.en` may improve recall on
  the harder cases and fits in 4 GB VRAM. Untested.
- **The web UI in `DESIGN.md` is not started**, by explicit instruction.
