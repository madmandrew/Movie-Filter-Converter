# Design notes — future build

Captured 2026-07-25. This is the intended shape of the real application, distinct from
the working pipeline prototype in `tools/`.

## Deployment

- **Web UI served from the Unraid server, in Docker.** Not a local desktop app.
- Needs GPU access for Whisper (`--gpus all` / Unraid's NVIDIA plugin). Confirm the
  Unraid box has an NVIDIA card — development so far has used the laptop's RTX 3050 Ti.
  If the server has no GPU, CPU `int8` works but is roughly 10-20x slower; a full-episode
  scan goes from ~1 minute to ~15.

## Library browsing

Scan and list media from the existing shares:

- `media/toFilter` (staging), `media/movies`, `media/tv`, `media/kids_*`
- For each title show:
  - **what filters are already applied** (needs persisted state — see Storage)
  - **whether a VidAngel filter exists** for it (tag-set lookup by title/year)
- Search across all of it.

## Filter run (per title)

1. User selects which VidAngel tags they want.
2. Run the audio pipeline (the `tools/` code: locate → tighten → verify).
3. **Video cuts from `start_approx` directly** — for nudity etc. the approximate
   6-second buckets are acceptable if padded ~1-2s each side. Video does not need the
   word-level precision that audio does, because there is no analogue of "half a
   syllable leaked".
4. Archive the original to `toFilter/unfilteredArchive/` so a run can be redone from
   the raw cut.
5. Write the filtered file **in place** (replacing the library copy).
6. Persist a report: what was filtered, scan results, what succeeded, what failed.

### Audio quality — hard requirement

Testing used `-c:a aac -b:a 448k`, which **destroys the DTS 5.1 track**. The real
build must preserve full quality.

Measured on Community S01E02 (DTS 5.1, 48 kHz, **1536 kbps**), 60 seconds of audio:

| option | size / 60s | lossless vs. source? |
|---|---|---|
| `-c:a copy` (baseline DTS) | 11.01 MB | n/a — cannot mute |
| FLAC (default level) | 20.29 MB | yes (of the decoded signal) |
| FLAC `-compression_level 12` | 20.26 MB | yes — max effort buys ~0.15% |
| TrueHD | 22.18 MB | yes, but larger than FLAC |
| EAC3 640k | 4.58 MB | no, lossy re-encode |

Notes that matter:

- **FLAC is ~1.84x the source size.** ~426 MB vs ~231 MB for a 21-min episode; roughly
  +1.2 GB of audio on a 2-hour film. Compression level is not worth tuning — the source
  is already-decoded lossy audio with little redundancy left.
- **ffmpeg's `dca` (DTS) and `truehd` encoders are flagged experimental (`X`)**. Do not
  round-trip a library through them.
- `-c:a copy` **cannot** apply mutes — filtering requires decoding.
- "Lossless" here means bit-exact to the *decoded* DTS, not to a hypothetical master.
  Since DTS is itself lossy, one decode is unavoidable; FLAC just adds nothing further.

**DECIDED (user, 2026-07-25): per-run choice of audio mode, exposed in the UI at filter
selection time.** TV shows can re-encode to the source codec (no size increase, no
audible loss); prized movies get lossless. Single filtered file either way, original
archived separately.

`--quality same` | `--quality lossless` in `run_filter.py`; the UI should surface this
as a per-title toggle, defaulting by library (TV → `same`, movies → `lossless`).

### Surgical splice — a third mode, measured and viable for lossy sources

Question raised by the user: can ffmpeg mute without re-encoding the whole track?
**Partly yes**, and it is better than either full-re-encode option where it applies.

Getting timestamps already never touches the original — `align.extract_audio()` writes
a temporary 16 kHz mono WAV and deletes it; the source is opened read-only throughout.

For the *cut*, audio codecs are frame-based (measured: DTS here is **constant 2012-byte
frames at ~10.67 ms**), so the track can be split into: everything before the mute
(stream copied), the muted span (re-encoded), everything after (stream copied), then
concatenated at the container level.

**Measured on a 40 s excerpt with one 0.43 s mute:**

- untouched regions vs. source: **`PSNR inf dB` on all 6 channels — bit-identical**
- only 81,920 of 7,824,096 bytes were re-encoded (**1%**), and that 1% is silence, so
  nothing of value is lost at all
- size delta: **+5 KB**, pure container overhead

Two real constraints found by testing:

1. **Boundary drift: +28.46 ms** on a single splice, because `-ss`/`-t` snap to frame
   boundaries and the segments no longer sum to the original duration. This is
   **cumulative** — ~40 mutes would drift over a second and progressively desync audio
   from video. Must be fixed by computing cut points as exact multiples of the codec
   frame duration (10.667 ms for DTS) rather than letting ffmpeg round, and asserting
   the total frame count matches.
2. **Does not work for lossless/object sources.** Segment A *does* keep DTS-HD MA
   through a stream copy — but segment B has to be re-encoded and there is no DTS-HD MA
   encoder, so it lands as FLAC, and **a single audio stream cannot change codec
   mid-file**. Splicing therefore requires that the muted span be re-encodable to the
   *same* codec: plain DTS, AC3, EAC3, AAC.

**Resulting mode matrix:**

| source | best mode | result |
|---|---|---|
| plain DTS / AC3 / EAC3 / AAC | **splice** | bit-identical outside mutes, size-neutral |
| DTS-HD MA / DTS:X / TrueHD+Atmos | **FLAC** (full re-encode) | bit-exact PCM, ~1.84x audio size |

**IMPLEMENTED** in `tools/splice.py`; `--quality splice` is now the default and falls
back to `lossless` automatically for lossless/object sources. Verified on the real
episode: 0.14% of bytes re-encoded, all copied regions byte-identical, all mutes silent,
+600 KB file size. See `STATUS.md` for the four measurement-driven fixes it needed
(output seek, byte-offset splicing, edge-frame padding, codec gating).

### Codec reality check — this constrains the UI

Surveyed the actual `toFilter` library. It is **not** plain DTS:

| source profile | channels | re-encodable? |
|---|---|---|
| DTS (plain, 1536k) | 6 | **yes** — `dca`, size-neutral, ~166 dB PSNR |
| AC3 384k | 6 | yes |
| DTS-HD MA | 6–8 | **no** — `dca` emits only the lossy core |
| DTS-HD MA + DTS:X | 8 | **no** — plus object metadata is unrecoverable |
| Dolby TrueHD + Atmos | 8 | **no** — encoder experimental, no Atmos support |

So `same` is only honest for plain lossy sources. For lossless/object sources
`audio_args()` **refuses `same` and forces FLAC**, printing a warning — otherwise the
"keep it the same" option would silently downgrade a DTS-HD MA track to its 1536k core,
which is the exact opposite of the user's intent for movies.

**Unavoidable loss to disclose in the UI:** Atmos/DTS:X **object metadata cannot survive
any filtering** — muting requires decoding to PCM, and there is no encoder to put the
objects back. For those titles the choice is "filtered 7.1 PCM/FLAC" or "unfiltered with
Atmos", not both. This is a genuine limitation, not an implementation gap.

- The filtered file carries **one audio track**: the filtered audio at **full quality
  (FLAC, lossless)**. No lossy re-encode, ever.
- The unfiltered original is kept as a **separate file** in
  `toFilter/unfilteredArchive/`.
- **Explicitly rejected: dual-track MKV** (original DTS + filtered FLAC in one
  container). The user does not want two tracks in one file — too easy to select the
  wrong one by accident. One file, one track, unambiguous.
- **Explicitly rejected: EDL-sidecar-as-default.** The mutes must be baked into the
  file, not applied player-side.
- The archive is **mandatory, not optional**: video cuts are destructive (frames are
  removed), so the original is the only way to re-run a filter from the raw cut.

Accepted cost: FLAC is ~1.84x the source audio size, and the archive doubles total
storage for filtered titles. The user has accepted both.

## Interactive scan review

The full-episode scan currently reports hits as advisory text. The UI should let the
user act on each hit:

- Show the hit **in context**: "...what the **hell** is this..."
- Yes / no per hit — "hello how are you" is rejected, "what the hell is this" accepted.
- **Word-boundary splitting — mostly a non-issue.** Measured: Whisper already emits
  `'the'` and `'hell'` as **separate tokens** in "what the hell are you", so the
  granularity the user wanted is native. No de-merging needed in the common case.

  What *does* go wrong is span accuracy, not tokenisation. In that same phrase Whisper
  gave `'the'` a **0 ms** span (932.120 -> 932.120) and `'hell'` **1040 ms** — it
  collapses one token and dumps the slack into its neighbour. The energy-refinement
  step in `locate.refine_edges` is what converts that into the real ~417 ms mute. So
  the UI must show and mute **energy-refined** bounds, never raw Whisper spans.

  For the rare genuine merge, fall back to proportional split by character count and
  let the user pick "mute whole token" vs. "mute just <word>". Low priority.
- Play the clip in-browser so the decision is informed by audio, not just text.

## Open questions

### Is VidAngel still needed for word filtering?

**For audio, essentially no.** Measured on S01E02, the full scan was *strictly better*
than the tag-set:

- it found all 5 tagged words in the chosen categories, **and**
- found a 6th "hell" at 15:34.320 that **VidAngel never tagged** (two utterances 1.4s
  apart; they caught one), **and**
- recovered the one incident (`hell` @ bucket 594) that the tag-set pointed at but the
  targeted per-incident search could not find.

A user-defined word list + full scan therefore dominates the tag-set for audio. It also
removes the dependency on VidAngel having covered a given title at all.

What VidAngel still contributes for audio, in descending order of value:

1. **Category taxonomy** — "hell" is `profanity`, "God" is `blasphemy`, "douche" is
   `language_sexual`. Useful for per-category opt-in that a flat word list can't express.
2. **A sanity-check count** — "VidAngel says 6 in this episode, we found 6" catches a
   systematically broken scan.
3. **Words not on the user's list** — their taggers flag things a hand-written list
   would omit (slang, references), though only the `key`-bearing categories name a word.

The timings themselves are near-worthless for audio: 6-second buckets with drift up to
+3s, and one pointing at a word that isn't in the local file at all.

**Practical conclusion:** build the word list as the primary source for audio, treat a
tag-set as optional enrichment when present. Do not block a filter run on VidAngel
having the title.

### Nudity / video detection

This is the real remaining dependency on VidAngel, and it should stay that way.

**Shot-boundary snapping works and is the biggest win available.** Measured on
S01E02 — 257 scene cuts detected in the episode, then compared against the 10
`audiovisual` buckets in the tag-set:

| bucket | nearest cut | delta |
|---|---|---|
| 276 | 275.859 | 141 ms |
| 528 | 528.278 | 278 ms |
| 744 | 743.826 | 174 ms |
| 750 | 750.333 | 333 ms |
| 186 | 184.81 | 1.19 s |
| 210 | 209.126 | 874 ms |
| 270, 534, 1254, 1260 | none within 4s | mid-shot tags |

**6 of 10 land within ~1.2s of a real shot boundary**, four of them inside 350 ms. So
VidAngel's video taggers were largely marking *shot boundaries*, which means snapping
their 6-second buckets to detected cuts recovers near-exact edges — the video analogue
of what energy-refinement does for audio. For the 4 mid-shot tags, fall back to
padding ~1-2s as the user already does by hand.

Detection command (fast, no GPU, whole episode in one pass):

```
ffprobe -v error -f lavfi -i "movie=<escaped path>,select=gt(scene\,0.35)" \
        -show_entries frame=best_effort_timestamp_time -of csv=p=0
```

Note: paths need `\` -> `/` and `:` -> `\\:` escaping inside the lavfi graph, and
ffprobe exits 255 at end-of-stream even on success.

**Automated nudity detection: possible, but keep VidAngel as primary.**

- NSFW classifiers (NudeNet, CLIP-based) over frames sampled at 1-2 fps, merging
  positive runs into ranges. Technically straightforward.
- But: false positives on swimwear/medical/art, false negatives on brief or partial
  nudity, and **no notion of narrative context** — it cannot distinguish "a scene the
  user wants cut" from "two frames of a painting". Unlike the word scan, there is no
  cheap ground-truth check to verify a hit, because there is no equivalent of "assert
  the word is gone".
- Verdict: usable only as a **discovery aid with mandatory human review**, never
  auto-applied. VidAngel's human curation is the thing being paid for here and it is
  not replaceable by a classifier.

**Recommendation: VidAngel for *what and roughly where*, shot detection for *exact
boundaries*, classifier optional as a review-gated discovery aid.**
