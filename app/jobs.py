"""
Background filter-run queue.

One worker thread, one job at a time: the GPU is a single resource and two concurrent
Whisper models would not fit in 4 GB VRAM. Progress is written to the DB so the UI can
poll it and the user can close the tab.

A run does, in order:
  1. archive the original (mandatory — video cuts are destructive)
  2. locate + verify each audio incident
  3. full-episode discovery scan for the word list
  4. resolve video ranges, snapping to scene cuts
  5. render the filtered file
  6. persist the report
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "tools"))

from db import connect, tx  # noqa: E402

_Q: "queue.Queue[int]" = queue.Queue()
_worker: threading.Thread | None = None
_current: dict = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enqueue(path: str, options: dict) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO runs(path, status, stage, options_json, created_at) "
            "VALUES (?,'queued','waiting',?,?)",
            (path, json.dumps(options), _now()),
        )
        run_id = cur.lastrowid
    _Q.put(run_id)
    ensure_worker()
    return run_id


def reap_orphans() -> int:
    """Fail runs left 'running' by a previous process.

    Nothing resumes a job across a restart — the worker is in-process — so a run still
    marked running at startup is an orphan. Left alone it shows as active forever, which
    is indistinguishable from a live job and makes the whole idea of "is it stuck?"
    meaningless.
    """
    # Build the message in Python: adjacent string literals concatenate in Python but are
    # a syntax error inside a SQL statement, and SQLite reports it only at execute time.
    note = ("Interrupted: the server restarted while this run was in progress. Nothing "
            "resumes across a restart — use \"Edit & re-run\" to start it again.")
    with tx() as c:
        rows = c.execute(
            "SELECT id FROM runs WHERE status IN ('running','queued')").fetchall()
        for r in rows:
            c.execute(
                "UPDATE runs SET status='failed', finished_at=?, "
                "error = COALESCE(error,'') || ? WHERE id=?",
                (_now(), note, r["id"]),
            )
    return len(rows)


def ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, daemon=True, name="filter-worker")
        _worker.start()


def _loop() -> None:
    while True:
        run_id = _Q.get()
        try:
            _execute(run_id)
        except Exception:
            _fail(run_id, traceback.format_exc())
        finally:
            _Q.task_done()
            _current.clear()


def _log(run_id: int, msg: str) -> None:
    # Timestamp every line: on a long run the gap between lines is what tells you whether
    # a stage is working or wedged.
    stamped = f"[{_now()[11:19]}] {msg}"
    with tx() as c:
        c.execute("UPDATE runs SET log = log || ?, heartbeat_at=? WHERE id=?",
                  (stamped + "\n", _now(), run_id))


def _stage(run_id: int, stage: str, pct: float) -> None:
    _current.update(run_id=run_id, stage=stage, pct=pct, at=_now())
    with tx() as c:
        c.execute("UPDATE runs SET stage=?, progress=?, heartbeat_at=? WHERE id=?",
                  (stage, pct, _now(), run_id))


def _fail(run_id: int, err: str) -> None:
    with tx() as c:
        c.execute(
            "UPDATE runs SET status='failed', error=?, finished_at=? WHERE id=?",
            (err[-4000:], _now(), run_id),
        )
        row = c.execute("SELECT path FROM runs WHERE id=?", (run_id,)).fetchone()
        if row:
            c.execute("UPDATE titles SET status='failed' WHERE path=?", (row["path"],))


def current() -> dict:
    return dict(_current)


def worker_alive() -> bool:
    """Is the job thread running?

    A dead worker and a wedged job look identical from the outside — both leave a run
    sitting at the same stage forever — but they need different responses, so report
    them separately.
    """
    return bool(_worker and _worker.is_alive())


def queue_depth() -> int:
    return _Q.qsize()


def _execute(run_id: int) -> None:
    conn = connect()
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or run["status"] == "cancelled":
        return
    path = run["path"]
    opts = json.loads(run["options_json"] or "{}")

    with tx() as c:
        c.execute("UPDATE runs SET status='running', started_at=? WHERE id=?",
                  (_now(), run_id))

    from align import get_model, probe_duration, probe_fps
    from locate import locate, snap_to_frames
    from scan import cross_reference, scan as full_scan
    from verify import tighten
    import vidangel

    _stage(run_id, "probing", 2)
    fps = probe_fps(path)
    duration = probe_duration(path)
    _log(run_id, f"fps={fps:.3f} duration={duration:.1f}s")

    # ---- optional tag-set -------------------------------------------------------
    ts = None
    if opts.get("tag_set_id"):
        row = conn.execute("SELECT payload FROM tagsets WHERE tag_set_id=?",
                           (opts["tag_set_id"],)).fetchone()
        if row:
            ts = vidangel.parse(row["payload"])
            warn = ts.check_runtime(duration)
            if warn:
                _log(run_id, f"WARNING {warn}")

    categories = tuple(opts.get("categories") or ())
    words = opts.get("words") or []

    # ---- archive ----------------------------------------------------------------
    archive = opts.get("archive_path")
    if archive:
        _stage(run_id, "archiving", 5)
        if os.path.exists(archive):
            _log(run_id, f"archive exists, reusing: {archive}")
        else:
            try:
                os.makedirs(os.path.dirname(archive), exist_ok=True)
                import shutil
                shutil.copy2(path, archive)
                _log(run_id, f"archived -> {archive}")
            except (OSError, PermissionError) as exc:
                # The archive is the only route back to the raw cut once video is cut, so
                # a failure here must stop the run rather than proceed unprotected. A
                # read-only media mount is the common cause.
                raise RuntimeError(
                    f"could not write the archive to {archive}: {exc}. The filtered file "
                    f"was NOT created. Point the archive at a writable location in "
                    f"Settings, or make the media share writable."
                ) from None

    model = get_model(opts.get("model", "small.en"))
    results: list[dict] = []
    mutes: list[tuple[str, float, float]] = []

    # ---- audio incidents from the tag-set ---------------------------------------
    todo = []
    source = "no tag-set"
    audio_refs = set(opts.get("audio_refs") or [])
    if ts:
        # Explicitly chosen refs are an exact instruction and are resolved against the
        # WHOLE tag-set. `only_enabled` is a convenience filter for "start from what
        # VidAngel already had on"; applying it first silently discarded every hand-picked
        # incident, then reported them as non-existent. A selection the user made by
        # ticking boxes must never be narrowed by a checkbox they left set.
        by_ref = [i for i in ts.incidents
                  if i.ref_id in audio_refs and i.kind == "audio"]

        pool = ts.enabled() if opts.get("only_enabled") else ts.incidents
        # Same trap for categories: a tag-set usually has a handful of enabled tags, so
        # intersecting them with a category choice tends to yield nothing.
        if (opts.get("only_enabled") and categories
                and not any(i.category_key in categories for i in pool)):
            _log(run_id, f"WARNING 'only tags enabled in VidAngel' left {len(pool)} tag(s), "
                         f"none in {categories}; using all {len(ts.incidents)} incidents "
                         f"instead")
            pool = ts.incidents
        by_cat = [i for i in pool if i.category_key in categories]

        if by_ref:
            # Individually-chosen incidents. Selecting one scene from a category and
            # leaving its siblings alone is only expressible per ref_id — a category
            # name cannot say "this rape reference but not that one".
            todo = by_ref
            source = "by ref"
        else:
            # Refs that match nothing usually mean they came from a different tag-set —
            # a re-run pointed at another cut, say. Falling back to the categories beats
            # silently filtering nothing, which is what a strict `if audio_refs` did.
            todo = by_cat
            source = str(categories)
            if audio_refs:
                _log(run_id, f"WARNING none of the {len(audio_refs)} selected incidents "
                             f"exist in tag-set {opts.get('tag_set_id')}; falling back to "
                             f"categories {categories or '(none)'}")
    _log(run_id, f"{len(todo)} tagged audio incidents ({source})")

    # Estimate the source-to-file offset BEFORE locating anything.
    #
    # A filter source is keyed to whatever cut the provider had, and a local rip can
    # differ by seconds to minutes. Rather than widen every search window (cost scales
    # with window size — a ±60s window is ~24s per incident), derive the offset from a
    # cheap scan of just the words the tags name, then search normally around corrected
    # positions. Measured exact to 0.0000s at offsets up to +250s.
    #
    # This is also why picking the "right" streaming offering barely matters: the timeline
    # is measured from the local audio, not trusted from the source.
    tag_offset = 0.0
    #: How far the tag-set's runtime may differ from the file before its timings are
    #: untrustworthy for video. Audio survives a mismatch because every word is located
    #: in the local track; video cannot be located at all, so a bad offset cuts the wrong
    #: footage — measured on a real run: a -27s mismatch put an 18s cut 4s past the end
    #: of the content it was meant to remove.
    RUNTIME_TOLERANCE = 10.0
    if todo and opts.get("auto_offset", True):
        _stage(run_id, "estimating source offset", 8)
        import offset as off_mod

        probe_words = sorted({w for i in todo for w in i.words})
        probe_hits = full_scan(path, probe_words, model=model, progress=False)
        est = off_mod.estimate(
            [(i.start_approx, i.words[0]) for i in todo if i.words],
            [(float(h.start), h.word) for h in probe_hits],
        )
        _log(run_id, f"offset estimate: {est.summary}")
        if est.confident:
            tag_offset = est.offset
        report_offset = {
            "offset": round(est.offset, 3), "support": est.support,
            "considered": est.considered, "spread": round(est.spread, 3),
            "confident": est.confident, "applied": round(tag_offset, 3),
        }
    else:
        report_offset = {}

    for n, inc in enumerate(todo):
        _stage(run_id, f"locating {inc.words[0]} @{inc.start_approx:.0f}s",
               10 + 30 * n / max(1, len(todo)))
        w0, w1 = inc.search_window()
        centre = inc.start_approx + tag_offset
        best = None
        for cand in inc.words:
            m = locate(path, cand, centre, centre, fps,
                       model=model, search_pad=(w1 - w0) / 2)
            if m and (best is None or m.confidence > best.confidence):
                best = m
        if best is None:
            results.append({"ref_id": inc.ref_id, "word": inc.words[0],
                            "bucket": inc.start_approx, "status": "NOT_FOUND"})
            continue
        s, e, v, rounds = tighten(path, best.expected, best.start, best.end, fps,
                                  model=model)
        mutes.append((inc.ref_id, s, e))
        results.append({
            "ref_id": inc.ref_id, "word": best.expected, "bucket": inc.start_approx,
            "start": round(s, 3), "end": round(e, 3),
            # Drift is measured against the *offset-corrected* position, so it reports how
            # far off the source's own timing was rather than restating a known offset.
            "drift": round(s - centre, 3),
            "confidence": round(best.confidence, 3), "rounds": rounds,
            "status": "OK" if v.ok else "REVIEW", "note": v.note,
        })

    # ---- full discovery scan ----------------------------------------------------
    scan_words = sorted(set(words) | {w for i in todo for w in i.words})
    scan_report: dict = {}
    if scan_words and opts.get("do_scan", True):
        _stage(run_id, f"scanning full audio for {len(scan_words)} words", 45)
        hits = full_scan(path, scan_words, model=model, progress=False)

        # Recover incidents the targeted pass missed. Whisper's chunking differs between
        # a narrow per-incident window and the full scan, and decode variance means the
        # scan sometimes hears a word the targeted search did not. If an uncovered hit
        # sits near an unresolved bucket, it is almost certainly that incident.
        from locate import _variants

        RECOVER_WINDOW = 8.0
        for r in [x for x in results if x["status"] == "NOT_FOUND"]:
            near = [
                h for h in hits
                if h.covered_by is None
                and h.word in _variants(r["word"])
                and abs(h.start - (r["bucket"] + tag_offset)) <= RECOVER_WINDOW
            ]
            if not near:
                continue
            h = min(near, key=lambda x: abs(x.start - (r["bucket"] + tag_offset)))
            s, e = snap_to_frames(max(0.0, h.start - 0.06), h.end + 0.06, fps)
            s, e, v, rounds = tighten(path, r["word"], s, e, fps, model=model)
            mutes.append((r["ref_id"], s, e))
            r.update(start=round(s, 3), end=round(e, 3),
                     drift=round(s - (r["bucket"] + tag_offset), 3),
                     confidence=round(h.confidence, 3), rounds=rounds,
                     status="OK_VIA_SCAN" if v.ok else "REVIEW", note=v.note)
            _log(run_id, f"recovered {r['ref_id']} {r['word']} via scan at {s:.3f} "
                         f"(drift {s - r['bucket']:+.2f}s)")

        covered, missed = cross_reference(hits, mutes)

        # Honour prior review decisions so a re-run doesn't re-ask.
        decided = {
            (round(r["at_time"], 2), r["word"]): r["action"]
            for r in conn.execute(
                "SELECT at_time, word, action FROM decisions WHERE path=?", (path,)
            ).fetchall()
        }
        auto, pending = [], []
        for h in missed:
            action = decided.get((round(h.start, 2), h.word))
            if action == "mute":
                auto.append(h)
            elif action == "skip":
                continue
            else:
                pending.append(h)

        for h in auto:
            s, e = snap_to_frames(max(0.0, h.start - 0.06), h.end + 0.06, fps)
            s, e, v, _r = tighten(path, h.word, s, e, fps, model=model)
            mutes.append((f"scan@{h.start:.2f}", s, e))
            results.append({"ref_id": f"scan@{h.start:.2f}", "word": h.word,
                            "bucket": None, "start": round(s, 3), "end": round(e, 3),
                            "status": "OK_FROM_WORDLIST", "note": v.note})

        scan_report = {
            "total_hits": len(hits),
            "covered": len(covered),
            "auto_muted": len(auto),
            "pending_review": [
                {"word": h.word, "at": round(h.start, 3), "end": round(h.end, 3),
                 "confidence": round(h.confidence, 2), "context": h.context}
                for h in pending
            ],
        }
        _log(run_id, f"scan: {len(hits)} hits, {len(covered)} covered, "
                     f"{len(auto)} auto-muted, {len(pending)} awaiting review")

    # ---- VideoSkip filter file --------------------------------------------------
    # Second-choice source when VidAngel has nothing. Audio entries carry the word in
    # their description, so they can be located precisely like a tagged incident; video
    # entries are real ranges (not 6s buckets) and are used as-is.
    vsk_ranges: list[dict] = []
    if opts.get("videoskip_id"):
        import videoskip_client as vsc

        row = conn.execute("SELECT payload FROM skipfiles WHERE id=?",
                           (opts["videoskip_id"],)).fetchone()
        if row:
            sk = vsc.parse_any(row["payload"])
            _log(run_id, f"VideoSkip filter: {len(sk.audio())} audio, "
                         f"{len(sk.video())} video entries")

            for n, ent in enumerate(sk.audio()):
                _stage(run_id, f"videoskip audio {n + 1}/{len(sk.audio())}",
                       50 + 8 * n / max(1, len(sk.audio())))
                ref = f"vsk{n}"
                word = (ent.description or "").strip()
                if word and " " not in word:
                    # A named word: locate it, so the Exchange's timing accuracy does
                    # not limit ours.
                    mt = locate(path, word, ent.start, ent.end, fps, model=model,
                                search_pad=3.0)
                    if mt:
                        s, e, v, rounds = tighten(path, word, mt.start, mt.end, fps,
                                                  model=model)
                        mutes.append((ref, s, e))
                        results.append({
                            "ref_id": ref, "word": word, "bucket": ent.start,
                            "start": round(s, 3), "end": round(e, 3),
                            "drift": round(s - ent.start, 3),
                            "confidence": round(mt.confidence, 3), "rounds": rounds,
                            "status": "OK_VIDEOSKIP" if v.ok else "REVIEW",
                            "note": v.note})
                        continue
                # No word, or not found: honour the file's own timing.
                s, e = snap_to_frames(max(0.0, ent.start), ent.end, fps)
                mutes.append((ref, s, e))
                results.append({
                    "ref_id": ref, "word": word or ent.category, "bucket": ent.start,
                    "start": round(s, 3), "end": round(e, 3),
                    "status": "OK_VIDEOSKIP",
                    "note": "used file timing (no word to locate)"})

            for ent in sk.video():
                vsk_ranges.append({"start": ent.start, "end": ent.end,
                                   "method": f"videoskip:{ent.category}"})

    # ---- manual entries ---------------------------------------------------------
    # Hand-specified mutes and cuts, for the cases where VidAngel has nothing and the
    # user knows exactly what they want gone.
    #
    # Two flavours of manual audio mute:
    #   * a word at an approximate time  -> located and verified like a tagged incident,
    #     so the user supplies a rough timestamp rather than frame-accurate boundaries
    #   * an explicit start/end range    -> muted exactly as given, no word-finding
    manual_mutes = opts.get("manual_mutes") or []
    for n, m in enumerate(manual_mutes):
        _stage(run_id, f"manual mute {n + 1}/{len(manual_mutes)}",
               60 + 5 * n / max(1, len(manual_mutes)))
        word = (m.get("word") or "").strip()
        ref = f"manual{n}"

        if word and m.get("at") is not None:
            at = float(m["at"])
            pad = float(m.get("search_pad", 5.0))
            mt = locate(path, word, at, at, fps, model=model, search_pad=pad)
            if mt is None:
                results.append({"ref_id": ref, "word": word, "bucket": at,
                                "status": "NOT_FOUND",
                                "note": f"no '{word}' within +/-{pad:.0f}s of {at:.1f}s"})
                _log(run_id, f"manual: '{word}' not found near {at:.1f}s")
                continue
            s, e, v, rounds = tighten(path, word, mt.start, mt.end, fps, model=model)
            mutes.append((ref, s, e))
            results.append({"ref_id": ref, "word": word, "bucket": at,
                            "start": round(s, 3), "end": round(e, 3),
                            "drift": round(s - at, 3),
                            "confidence": round(mt.confidence, 3), "rounds": rounds,
                            "status": "OK_MANUAL" if v.ok else "REVIEW",
                            "note": v.note})
            _log(run_id, f"manual: '{word}' -> {s:.3f}-{e:.3f}")
        else:
            # Explicit range: trust the user, but still snap to frame boundaries so the
            # mute cannot land mid-frame.
            s = float(m["start"])
            e = float(m["end"])
            s, e = snap_to_frames(max(0.0, s), e, fps)
            mutes.append((ref, s, e))
            results.append({"ref_id": ref, "word": m.get("label") or "(manual range)",
                            "bucket": None, "start": round(s, 3), "end": round(e, 3),
                            "status": "OK_MANUAL", "note": "explicit range, not verified"})
            _log(run_id, f"manual range mute {s:.3f}-{e:.3f}")

    # ---- video ranges -----------------------------------------------------------
    video_ranges: list[dict] = []
    manual_cuts = opts.get("manual_cuts") or []
    # Only true if something will actually resolve to a cut. Checking the *presence* of
    # video_refs was not enough: refs from a different tag-set match nothing, and the run
    # still paid for a full-decode scene-detection pass to place zero cuts — 25 minutes
    # of CPU on an 87-minute film for no output.
    want_tagged_video = False
    if ts:
        _vcats = set(opts.get("video_categories") or ())
        _vrefs = set(opts.get("video_refs") or [])
        want_tagged_video = any(
            i.kind == "audiovisual" and not i.is_structural
            and (i.ref_id in _vrefs
                 or i.category_key in _vcats or i.category_title in _vcats)
            for i in ts.incidents
        )
        if (_vcats or _vrefs) and not want_tagged_video:
            _log(run_id, "no video incidents matched the selection; skipping scene "
                         "detection")

    # NudeNet discovery. Advisory like the word scan: a classifier has no notion of
    # narrative context, so detections are surfaced for a decision rather than cut
    # automatically. Approved ones come back through `decisions` on a re-run.
    nudity_report: dict = {}
    nudity_ranges: list[dict] = []
    if opts.get("detect_nudity"):
        _stage(run_id, "scanning for nudity", 62)
        import nudity as nud

        # An explicit window scans one span instead of the whole film. `scan_video`
        # reports absolute timestamps either way (it offsets sampled frames by `start`),
        # so review decisions keyed on those timestamps stay valid across a re-run with a
        # different window.
        n_start = opts.get("nudity_start")
        n_end = opts.get("nudity_end")
        n_start = float(n_start) if n_start is not None else 0.0
        n_end = float(n_end) if n_end is not None else None
        if n_end is not None and duration:
            n_end = min(n_end, duration)

        found = nud.scan_video(
            path,
            sample_fps=float(opts.get("nudity_fps", 1.0)),
            min_score=float(opts.get("nudity_min_score", nud.MIN_SCORE)),
            min_hits=int(opts.get("nudity_min_hits", 2)),
            start=n_start,
            end=n_end,
        )
        if n_start or n_end is not None:
            _log(run_id, f"nudity scan window: {n_start:.1f}s - "
                         f"{f'{n_end:.1f}s' if n_end is not None else 'end'}")
        _log(run_id, f"nudity scan: {len(found)} candidate ranges")

        decided = {
            round(r["at_time"], 1): r["action"]
            for r in conn.execute(
                "SELECT at_time, action FROM decisions WHERE path=? AND word='__nudity__'",
                (path,),
            ).fetchall()
        }
        pending, approved = [], []
        for r in found:
            action = decided.get(round(r.start, 1))
            if action == "mute":            # "mute" means "cut" for a video range
                approved.append(r)
            elif action != "skip":
                pending.append(r)

        nudity_report = {
            "candidates": len(found),
            "approved": len(approved),
            # Recorded so a reviewer can tell "nothing found" apart from "nothing found
            # *in the part that was scanned*" — the two look identical in the UI
            # otherwise, and only one of them means the title is clean.
            "window": ([round(n_start, 2), round(n_end, 2) if n_end is not None else None]
                       if (n_start or n_end is not None) else None),
            "pending_review": [
                {"start": round(r.start, 3), "end": round(r.end, 3),
                 "classes": r.classes,
                 "peak": (r.peak.cls if r.peak else None),
                 "score": round(r.peak.score, 2) if r.peak else None,
                 "hits": len(r.detections)}
                for r in pending
            ],
        }
        for r in approved:
            nudity_ranges.append({"start": r.start, "end": r.end,
                                  "method": "nudity:approved"})
        if pending:
            _log(run_id, f"  {len(pending)} nudity ranges awaiting review, "
                         f"{len(approved)} approved")

    if want_tagged_video or manual_cuts or vsk_ranges or nudity_ranges:
        from scenes import detect_cuts, merge_ranges, snap_range

        # Scene detection is one full pass over the video, so only run it if something
        # will actually use it: tagged ranges always snap, manual ranges only on request.
        # Nudity ranges always snap: detection is sampled (typically 1 fps) so the true
        # extent is wider than the first and last hit, and cutting to shot boundaries
        # removes the whole scene rather than a fragment of it.
        need_cuts = (want_tagged_video or bool(nudity_ranges) or bool(vsk_ranges)
                     or any(m.get("snap") for m in manual_cuts))
        cuts: list[float] = []
        if need_cuts:
            _stage(run_id, "detecting scene cuts", 70)

            # Report progress through the pass. It is one full decode of the file and
            # emits nothing until finished — on an 87-minute film that is ~25 minutes of
            # silence, which is indistinguishable from a hang in the live view.
            last_beat = [0.0]

            def _cut_progress(at: float, found: int) -> None:
                if at - last_beat[0] < 30.0:
                    return
                last_beat[0] = at
                pct = (at / duration * 100.0) if duration else 0.0
                _stage(run_id,
                       f"detecting scene cuts — {pct:.0f}% of the file, {found} found",
                       70)

            cuts = detect_cuts(path, progress=_cut_progress)
            _log(run_id, f"{len(cuts)} scene cuts detected")

        vr = []
        # Refuse tagged video cuts when the timeline is unverified.
        #
        # A video range cannot be located in the file the way a word can, so it inherits
        # the source's timing wholesale. If the tag-set is keyed to a different cut and no
        # audio offset was measured to correct it, the cut lands on the wrong footage —
        # removing good material and leaving the content it targeted. Better to skip and
        # say so than to damage the file while reporting success.
        delta = duration - (ts.runtime_unaltered or 0) if ts else 0.0
        unverified = (
            want_tagged_video
            and abs(delta) > RUNTIME_TOLERANCE
            and not (report_offset or {}).get("confident")
        )
        if unverified:
            _log(run_id,
                 f"SKIPPING video cuts: the tag-set runtime differs from this file by "
                 f"{delta:+.0f}s and no audio offset could be measured to correct it. "
                 f"Video ranges cannot be located in the file, so cutting now would "
                 f"remove the wrong footage. Enable the word-list scan or select audio "
                 f"incidents so an offset can be derived, or add the cut manually.")
            want_tagged_video = False

        if want_tagged_video:
            wanted = set(opts.get("video_categories") or ())
            video_refs = set(opts.get("video_refs") or [])
            # Same fallback as the audio path: refs matching nothing (a re-run pointed at
            # a different tag-set) would otherwise silently cut nothing at all.
            usable_refs = video_refs & {i.ref_id for i in ts.incidents}
            if video_refs and not usable_refs:
                _log(run_id, f"WARNING none of the {len(video_refs)} selected video "
                             f"incidents exist in this tag-set; falling back to "
                             f"categories {sorted(wanted) or '(none)'}")
            for inc in ts.incidents:
                if inc.kind != "audiovisual" or inc.is_structural:
                    continue
                if usable_refs:
                    if inc.ref_id not in usable_refs:
                        continue
                elif inc.category_key not in wanted and inc.category_title not in wanted:
                    continue
                # Video ranges cannot be located by transcription, so they rely entirely
                # on the offset estimated from the audio tags. Without it a wrong-cut
                # source would cut the wrong scene outright.
                vr.append(snap_range(
                    inc.start_approx + tag_offset,
                    max(inc.end_approx, inc.start_approx + vidangel.BUCKET_SECONDS)
                    + tag_offset,
                    cuts, duration=duration,
                ))

        # VideoSkip video entries are real timestamps, so a nominal pad is enough.
        for extra in vsk_ranges:
            vr.append(snap_range(extra["start"], extra["end"], cuts,
                                 duration=duration, pad=1.0))

        # Nudity ranges under-report their true extent, and by more than the sampling
        # interval alone. Two effects stack: a hit can land up to one interval late, and
        # `min_hits` discards the leading hits until the threshold is met. Measured: a
        # 2 fps scan with min_hits=2 reported 15.5s for content that starts at 13.5s —
        # 2.0s, which is (min_hits + 2) intervals, not one.
        #
        # Padding by that much means a cut opens before the scene rather than inside it.
        # Snapping still overrides the pad wherever a real shot boundary is closer, so on
        # normal footage this only matters for mid-shot content.
        _nfps = max(float(opts.get("nudity_fps", 1.0)), 0.1)
        _nhits = max(int(opts.get("nudity_min_hits", 2)), 1)
        nud_pad = max(1.0, (_nhits + 2) / _nfps)
        for extra in nudity_ranges:
            vr.append(snap_range(extra["start"], extra["end"], cuts,
                                 duration=duration, pad=nud_pad))

        for m in manual_cuts:
            s, e = float(m["start"]), float(m["end"])
            if m.get("snap"):
                vr.append(snap_range(s, e, cuts, duration=duration,
                                     pad=float(m.get("pad", 0.0))))
            else:
                from scenes import VideoRange

                vr.append(VideoRange(start=max(0.0, s), end=min(duration, e),
                                     method="manual", approx_start=s, approx_end=e))
            _log(run_id, f"manual cut {s:.1f}-{e:.1f}"
                         f"{' (snapped)' if m.get('snap') else ''}")

        for r in merge_ranges(vr):
            video_ranges.append({"start": round(r.start, 3), "end": round(r.end, 3),
                                 "method": r.method})
        _log(run_id, f"{len(video_ranges)} video ranges after merging")

    mutes.sort(key=lambda m: m[1])
    report = {
        "path": path, "fps": fps, "duration": duration,
        "incidents": results, "mutes": mutes,
        "scan": scan_report, "video_ranges": video_ranges,
        "nudity": nudity_report,
        "offset": report_offset,
        "options": opts,
    }

    # ---- render -----------------------------------------------------------------
    out = opts.get("output_path")
    if out and (mutes or video_ranges):
        _stage(run_id, "rendering", 85)
        import render as render_mod

        stats = render_mod.render(path, out, mutes, video_ranges,
                                  quality=opts.get("quality", "splice"))
        report["render"] = stats
        _log(run_id, f"rendered {out}: {stats.get('summary','')}")

        # Verify the cuts actually removed what they were meant to. Cutting shifts the
        # timeline, so the region to re-check is where each removed range *used to be* —
        # after the cut that is the join point, i.e. the start minus everything removed
        # before it.
        if nudity_ranges and opts.get("verify_nudity", True):
            _stage(run_id, "verifying nudity removed", 95)
            import nudity as nud

            ordered = sorted(video_ranges, key=lambda c: c["start"])
            check: list[tuple[float, float]] = []
            for nr in nudity_ranges:
                removed_before = sum(
                    min(c["end"], nr["start"]) - c["start"]
                    for c in ordered if c["start"] < nr["start"]
                )
                joint = max(0.0, nr["start"] - removed_before)
                # Inspect a couple of seconds either side of the join.
                check.append((max(0.0, joint - 1.5), joint + 1.5))

            survivors = nud.verify_absent(out, check)

            # A survivor at a join only means the cut failed if that content was in scope
            # to begin with. On a windowed scan, nudity just outside the window is
            # expected to remain — it was never detected, so it was never approved — and
            # reporting it as "not clean" would read as a broken cut rather than as the
            # window doing exactly what was asked.
            def _to_source(out_t: float) -> float:
                """Map an output timestamp back onto the source timeline.

                Cuts must be walked in order against a *running* output position: each
                removed range shifts everything after it, so comparing an output time
                directly against a source-time cut boundary mixes the two timelines and
                lands inside ranges that were actually removed.
                """
                src = out_t
                for c in ordered:
                    if c["start"] <= src:
                        src += c["end"] - c["start"]
                return src

            in_scope = []
            out_of_scope = []
            for s in survivors:
                src_t = _to_source(s.time)
                windowed = nudity_report.get("window")
                if windowed:
                    w_start, w_end = windowed[0] or 0.0, windowed[1]
                    if src_t < w_start or (w_end is not None and src_t > w_end):
                        out_of_scope.append(s)
                        continue
                in_scope.append(s)

            report["nudity_verify"] = {
                "checked_regions": len(check),
                "survivors": [
                    {"at": round(s.time, 3), "class": s.cls, "score": round(s.score, 2)}
                    for s in in_scope
                ],
                "outside_scan_window": [
                    {"at": round(s.time, 3), "class": s.cls, "score": round(s.score, 2)}
                    for s in out_of_scope
                ],
                "clean": not in_scope,
            }
            if in_scope:
                _log(run_id, f"  WARNING: {len(in_scope)} nudity detections remain in "
                             f"the output — review before keeping this file")
            else:
                _log(run_id, f"  verified: no nudity detected at {len(check)} cut joins")
            if out_of_scope:
                _log(run_id, f"  note: {len(out_of_scope)} detections remain outside the "
                             f"scan window — re-run over the whole film to catch them")

    _stage(run_id, "done", 100)
    with tx() as c:
        c.execute(
            "UPDATE runs SET status='done', report_json=?, finished_at=? WHERE id=?",
            (json.dumps(report), _now(), run_id),
        )
        c.execute(
            """UPDATE titles SET status='filtered', filtered_at=?, report_json=?,
                                 archive_path=?, tag_set_id=?
               WHERE path=?""",
            (_now(), json.dumps(report), archive, opts.get("tag_set_id"), path),
        )
