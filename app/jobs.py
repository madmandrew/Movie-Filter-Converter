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
    with tx() as c:
        c.execute("UPDATE runs SET log = log || ? WHERE id=?", (msg + "\n", run_id))


def _stage(run_id: int, stage: str, pct: float) -> None:
    _current.update(run_id=run_id, stage=stage, pct=pct)
    with tx() as c:
        c.execute("UPDATE runs SET stage=?, progress=? WHERE id=?", (stage, pct, run_id))


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

    categories = tuple(opts.get("categories") or vidangel.DEFAULT_CATEGORIES)
    words = opts.get("words") or []

    # ---- archive ----------------------------------------------------------------
    archive = opts.get("archive_path")
    if archive:
        _stage(run_id, "archiving", 5)
        if os.path.exists(archive):
            _log(run_id, f"archive exists, reusing: {archive}")
        else:
            os.makedirs(os.path.dirname(archive), exist_ok=True)
            import shutil
            shutil.copy2(path, archive)
            _log(run_id, f"archived -> {archive}")

    model = get_model(opts.get("model", "small.en"))
    results: list[dict] = []
    mutes: list[tuple[str, float, float]] = []

    # ---- audio incidents from the tag-set ---------------------------------------
    todo = []
    if ts:
        pool = ts.enabled() if opts.get("only_enabled") else ts.incidents
        todo = [i for i in pool if i.category_key in categories]
    _log(run_id, f"{len(todo)} tagged audio incidents in {categories}")

    for n, inc in enumerate(todo):
        _stage(run_id, f"locating {inc.words[0]} @{inc.start_approx:.0f}s",
               10 + 30 * n / max(1, len(todo)))
        w0, w1 = inc.search_window()
        best = None
        for cand in inc.words:
            m = locate(path, cand, inc.start_approx, inc.start_approx, fps,
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
            "drift": round(s - inc.start_approx, 3),
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
                and abs(h.start - r["bucket"]) <= RECOVER_WINDOW
            ]
            if not near:
                continue
            h = min(near, key=lambda x: abs(x.start - r["bucket"]))
            s, e = snap_to_frames(max(0.0, h.start - 0.06), h.end + 0.06, fps)
            s, e, v, rounds = tighten(path, r["word"], s, e, fps, model=model)
            mutes.append((r["ref_id"], s, e))
            r.update(start=round(s, 3), end=round(e, 3),
                     drift=round(s - r["bucket"], 3),
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

    # ---- video ranges -----------------------------------------------------------
    video_ranges: list[dict] = []
    if ts and opts.get("video_categories"):
        _stage(run_id, "detecting scene cuts", 70)
        from scenes import detect_cuts, merge_ranges, snap_range

        cuts = detect_cuts(path)
        _log(run_id, f"{len(cuts)} scene cuts detected")
        wanted = set(opts["video_categories"])
        vr = []
        for inc in ts.incidents:
            if inc.kind != "audiovisual" or inc.is_structural:
                continue
            if inc.category_key not in wanted and inc.category_title not in wanted:
                continue
            vr.append(snap_range(
                inc.start_approx,
                max(inc.end_approx, inc.start_approx + vidangel.BUCKET_SECONDS),
                cuts, duration=duration,
            ))
        for r in merge_ranges(vr):
            video_ranges.append({"start": round(r.start, 3), "end": round(r.end, 3),
                                 "method": r.method})
        _log(run_id, f"{len(video_ranges)} video ranges after merging")

    mutes.sort(key=lambda m: m[1])
    report = {
        "path": path, "fps": fps, "duration": duration,
        "incidents": results, "mutes": mutes,
        "scan": scan_report, "video_ranges": video_ranges,
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
