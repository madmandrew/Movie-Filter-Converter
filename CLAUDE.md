# CLAUDE.md

> **Read `STATUS.md` first.** As of 2026-07-25 the project has pivoted: a working
> Whisper-based audio pipeline lives in `tools/` and supersedes the app described
> below. `STATUS.md` is the handoff; `DESIGN.md` is the intended future build. This
> file documents the **legacy 2022 React app** in `src/`, kept as reference for its
> output formats and domain knowledge.

## What this project is (legacy app)

A single-page React app that converts **ClearPlay / VidAngel-style filter data** (pasted in as raw JSON) into
skip-list formats the user can apply to their own video files for a **Plex** server:

- **VideoSkip** format (`.vsk`-style: `HH:MM:SS.mmm --> HH:MM:SS.mmm` + a category line)
- **EDL** format (`start end action` in seconds — Plex/MPlayer style)

The workflow it was built for: paste two JSON blobs scraped from a filtering service, tick which
incidents to keep, set a sync offset, hit Convert, copy the output text out of a textarea, then
**manually** use those timestamps to mute/cut audio/video from local media files.

History: two commits, both **2022-08-18**. Untouched since. Written by the user as a personal
one-off tool. The user has said they want to **update or fully re-write it**.

## Repo layout

```
src/
  index.tsx                        CRA entry
  App.tsx                          renders <FilterConverter/> inside a full-viewport header
  components/
    FilterConverter.tsx            the entire UI (only real screen)
    FilterIncident.tsx             one checkbox + context text row
    FilterTypes.ts                 input JSON interfaces + category enums/map
    FilterUtils.ts                 all conversion logic (the valuable part)
    *.scss
public/                            stock CRA template assets
```

There is **no** router, no state manager, no backend, no persistence, no CI.

## Data model (what the app expects to be pasted in)

Two separate JSON documents, pasted into two separate textareas:

1. **"Filter SettingUI"** → `FilterSettings`
   - `asset: { name, duration }`
   - `filterSettingsUI.category[]` → `Category { id, desc, subcategory[] }`
   - `subcategory[].incident[]` → `Incident { id, context, desc }`
   - `category.desc` must match the `ClearplayCategories` enum values verbatim
     (`'Sex/Nudity' | 'Violence' | 'Language' | 'Substance Abuse'`).
2. **"Filter"** → `Filter`
   - `eventList[] : { id, interrupt, resume }` — `interrupt`/`resume` are **seconds**.

The join is `Incident.id === eventList[].id`. Doc 1 supplies the human-readable taxonomy and
description; doc 2 supplies the actual timestamps.

## Conversion logic (`FilterUtils.ts`)

- `formatFilterSettings` — flattens `category → subcategory → incident` into
  `ClearplayFilterGroup[] { category, filters: FilterOption[] }`, drops subcategories with no
  incidents, dedupes by `incident.id` via lodash `uniqWith`, and defaults every incident to
  `selected: true`.
- `convertToTimestamp(seconds)` — `new Date(s * 1000).toISOString().slice(11, 22)` → `HH:MM:SS.mmm`.
  **Breaks for runtimes ≥ 24h** (irrelevant here) and silently depends on UTC.
- `convertToVideoSkip` — emits per incident:
  `HH:MM:SS.mmm --> HH:MM:SS.mmm\n<VideoSkipCategory> 1 (<incident.context>)\n`
  Category is mapped through `ClearplayToVideoSkipCategoryMap`. The literal `1` is hardcoded.
- `convertToEDLFormat` — emits `<interrupt+offset> <resume+offset> <type>` where type is
  `1` for `Language` (EDL mute) and `0` for everything else (EDL cut). Seconds are raw, un-normalized.
- `offset` is added to both ends of every event, uniformly. There is no per-event nudging.

## Tech stack / state

- **Create React App 5.0.1** + `react-scripts` — deprecated and unmaintained; this is the single
  biggest reason a re-write is reasonable.
- React 18.2, TypeScript 4.7, MUI 5.8/5.10, lodash, `sass`.
- `react-json-view@1.21.3` — abandoned, throws under React 18 StrictMode in some paths.
- `App.test.tsx` is the **stock CRA "learn react" test** and will fail — it asserts on a link this
  app doesn't render. Effectively zero test coverage.
- No lockfile issues noted, but `npm install` on modern Node will hit CRA peer/OpenSSL problems.

## Known rough edges (all confirmed by reading the code)

1. `JSON.parse` on every keystroke in both textareas, with **no try/catch** — typing or pasting
   partial JSON throws an uncaught error and blanks the render.
2. Initial state is `useState<FilterSettings>({} as any)` — `formatFilterSettings` would throw on
   `undefined.category` if ever called before a valid paste.
3. `.map()` used purely for side effects / as `.forEach` in both converters (lines 43, 65).
4. Missing React `key` props on the `<Accordion>` and `<FilterIncident>` lists.
5. Offset `TextField` → `Number(e.target.value)` yields `NaN` for empty/garbage input, which then
   poisons every timestamp.
6. Output textareas are `value=`-bound with no `onChange` → React read-only warning; no copy button,
   no file download.
7. Everything lives in one component; conversion is not unit-tested at all.
8. Category matching is by **display string**, so any upstream wording change silently drops a
   whole category.
9. `FilterConverter.scss` has `.json-style` / `.json-text-input` rules that are dead — the component
   uses an inline `jsonViewerStyle` object instead.

## If re-writing

Preserve the two output formats and the id-join semantics — that's the actual domain knowledge here.
Worth considering: Vite instead of CRA, real Zod parsing of the pasted JSON with friendly errors,
file upload + download instead of copy/paste textareas, per-event offset, unit tests around
`convertToVideoSkip`/`convertToEDLFormat`, and emitting an ffmpeg command or Plex-ready `.edl`
directly so the "manual" step goes away.

## Commands

```
npm start     # dev server (CRA, port 3000)
npm run build
npm test      # currently fails: stock CRA test
```
