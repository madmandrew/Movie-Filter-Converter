"""
FastAPI application: library browsing, filter runs, and scan review.

Serves a small server-rendered UI plus a JSON API the page polls for run progress.
The filter pipeline in `tools/` is imported directly — same process, no IPC — so the
GPU model stays loaded between runs.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "tools"))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import db  # noqa: E402
import jobs  # noqa: E402
import library  # noqa: E402

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create the schema and start the job worker before serving any request."""
    db.init()
    jobs.ensure_worker()
    yield


app = FastAPI(title="Movie Filter", lifespan=lifespan)


app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    with open(os.path.join(_HERE, "templates", "index.html"), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------- library

@app.get("/api/library")
def api_library(q: str = "", lib: str | None = None, status: str | None = None,
                limit: int = 200, offset: int = 0):
    return {
        "items": library.search(q, lib, status, limit, offset),
        "stats": library.stats(),
        "roots": library.roots(),
    }


@app.post("/api/library/scan")
def api_scan():
    return library.scan()


@app.get("/api/title")
def api_title(path: str):
    try:
        info = library.ensure_probed(path)
    except KeyError:
        raise HTTPException(404, "unknown title")
    if info.get("report_json"):
        info["report"] = json.loads(info["report_json"])
    info.pop("report_json", None)

    # Which cached tag-sets plausibly match this title, by name overlap.
    rows = db.connect().execute(
        "SELECT tag_set_id, work_id, title_hint, runtime FROM tagsets"
    ).fetchall()
    name = (info.get("name") or "").lower()
    info["tagsets"] = [
        dict(r) for r in rows
        if not r["title_hint"] or _looks_like(r["title_hint"], name)
    ]
    return info


def _looks_like(hint: str, name: str) -> bool:
    """Loose title match: every significant word of the hint appears in the filename."""
    words = [w for w in hint.lower().replace(".", " ").split() if len(w) > 2]
    return bool(words) and all(w in name for w in words)


# -------------------------------------------------------------------- tag-sets

class TagSetIn(BaseModel):
    payload: str
    title_hint: str | None = None


@app.post("/api/tagsets")
def api_add_tagset(body: TagSetIn):
    """Store a VidAngel payload pasted in by the user.

    The server has no route to api.vidangel.com (it is account-authenticated), so
    payloads are supplied manually and cached here for reuse.
    """
    import vidangel

    try:
        ts = vidangel.parse(body.payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not parse payload: {exc}")

    with db.tx() as c:
        c.execute(
            """INSERT INTO tagsets(tag_set_id, work_id, title_hint, runtime, payload,
                                   added_at)
               VALUES (?,?,?,?,?,datetime('now'))
               ON CONFLICT(tag_set_id) DO UPDATE SET
                   payload=excluded.payload, title_hint=excluded.title_hint,
                   runtime=excluded.runtime""",
            (ts.tag_set_id, ts.work_id, body.title_hint, ts.runtime_unaltered,
             body.payload),
        )
    return {"tag_set_id": ts.tag_set_id, "incidents": len(ts.incidents),
            "enabled": len(ts.enabled()), "runtime": ts.runtime_unaltered}


@app.get("/api/tagsets/{tag_set_id}")
def api_tagset(tag_set_id: int):
    import vidangel

    row = db.connect().execute(
        "SELECT payload FROM tagsets WHERE tag_set_id=?", (tag_set_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "unknown tag-set")
    ts = vidangel.parse(row["payload"])

    groups: dict[str, dict] = {}
    for inc in ts.incidents:
        # Several distinct categories are titled just "other" ("other_racial",
        # "other_sexual", "other_childish"), which is indistinguishable in a list. Use
        # the parent category for those, and prefer the uncensored key over VidAngel's
        # asterisked display title ("h*ll" -> "hell").
        title = inc.category_title
        if title.lower() in ("other", "") or "*" in title:
            parent = inc.parents[-1] if inc.parents else ""
            title = (f"{parent} — other" if title.lower() == "other" and parent
                     else inc.category_key.replace("_", " "))
        g = groups.setdefault(
            inc.category_key,
            {"key": inc.category_key, "title": title,
             "kind": inc.kind, "locatable": inc.locatable, "incidents": []},
        )
        g["incidents"].append({
            "ref_id": inc.ref_id, "description": inc.description,
            "start": inc.start_approx, "end": inc.end_approx,
            "kind": inc.kind, "enabled": inc.enabled,
            "words": inc.words, "locatable": inc.locatable,
            "structural": inc.is_structural,
        })
    return {"tag_set_id": ts.tag_set_id, "runtime": ts.runtime_unaltered,
            "groups": sorted(groups.values(), key=lambda g: g["title"])}


# ------------------------------------------------------------------ word list

@app.get("/api/words")
def api_words():
    rows = db.connect().execute(
        "SELECT word, category, enabled FROM wordlist ORDER BY category, word"
    ).fetchall()
    return {"words": [dict(r) for r in rows]}


class WordIn(BaseModel):
    word: str
    category: str = "profanity"
    enabled: bool = True


@app.post("/api/words")
def api_add_word(body: WordIn):
    w = body.word.strip().lower()
    if not w.isalpha():
        raise HTTPException(400, "word must be alphabetic")
    with db.tx() as c:
        c.execute(
            "INSERT INTO wordlist(word,category,enabled) VALUES (?,?,?) "
            "ON CONFLICT(word) DO UPDATE SET category=excluded.category, "
            "enabled=excluded.enabled",
            (w, body.category, int(body.enabled)),
        )
    return {"ok": True}


@app.delete("/api/words/{word}")
def api_del_word(word: str):
    with db.tx() as c:
        c.execute("DELETE FROM wordlist WHERE word=?", (word.lower(),))
    return {"ok": True}


# ----------------------------------------------------------------------- runs

class RunIn(BaseModel):
    path: str
    tag_set_id: int | None = None
    categories: list[str] = []
    video_categories: list[str] = []
    words: list[str] | None = None
    quality: str = "splice"
    model: str = "small.en"
    do_scan: bool = True
    only_enabled: bool = False
    output_path: str | None = None
    archive_path: str | None = None


@app.post("/api/runs")
def api_run(body: RunIn):
    if not os.path.exists(body.path):
        raise HTTPException(404, "file not found")

    opts = body.model_dump()
    if opts["words"] is None:
        opts["words"] = db.enabled_words()

    # Default output/archive paths: filtered file replaces the library copy, original
    # goes to unfilteredArchive next to the configured toFilter root.
    if not opts["output_path"]:
        stem, ext = os.path.splitext(body.path)
        opts["output_path"] = f"{stem}.FILTERED{ext}"
    if not opts["archive_path"]:
        base = os.path.basename(body.path)
        root = library.roots().get("toFilter", os.path.dirname(body.path))
        opts["archive_path"] = os.path.join(root, "unfilteredArchive", base)

    run_id = jobs.enqueue(body.path, opts)
    return {"run_id": run_id}


@app.get("/api/runs")
def api_runs(limit: int = 50):
    rows = db.connect().execute(
        "SELECT id,path,status,stage,progress,created_at,finished_at,error "
        "FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return {"runs": [dict(r) for r in rows], "current": jobs.current()}


@app.get("/api/runs/{run_id}")
def api_run_detail(run_id: int):
    row = db.connect().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown run")
    d = dict(row)
    for key in ("report_json", "options_json"):
        if d.get(key):
            d[key.replace("_json", "")] = json.loads(d[key])
        d.pop(key, None)
    return d


@app.post("/api/runs/{run_id}/cancel")
def api_cancel(run_id: int):
    """Cancel a run that has not started yet.

    A running job is not interrupted — ffmpeg and Whisper are mid-write, and killing
    them could leave a partial output file next to a real library file.
    """
    with db.tx() as c:
        row = c.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown run")
        if row["status"] != "queued":
            raise HTTPException(409, f"cannot cancel a {row['status']} run")
        c.execute("UPDATE runs SET status='cancelled' WHERE id=?", (run_id,))
    return {"ok": True}


# --------------------------------------------------------------------- review

@app.get("/api/review/{run_id}")
def api_review(run_id: int):
    """Scan hits from a finished run that still need a yes/no decision."""
    row = db.connect().execute(
        "SELECT path, report_json FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if not row or not row["report_json"]:
        raise HTTPException(404, "no report for this run")
    report = json.loads(row["report_json"])
    return {"path": row["path"],
            "pending": report.get("scan", {}).get("pending_review", [])}


class DecisionIn(BaseModel):
    path: str
    at_time: float
    word: str
    action: str          # mute | skip
    note: str | None = None


@app.post("/api/review")
def api_decide(body: DecisionIn):
    if body.action not in ("mute", "skip"):
        raise HTTPException(400, "action must be 'mute' or 'skip'")
    with db.tx() as c:
        c.execute(
            "INSERT INTO decisions(path,at_time,word,action,note) VALUES (?,?,?,?,?) "
            "ON CONFLICT(path,at_time,word) DO UPDATE SET action=excluded.action, "
            "note=excluded.note",
            (body.path, round(body.at_time, 2), body.word, body.action, body.note),
        )
    return {"ok": True}


@app.get("/api/clip")
def api_clip(path: str, start: float, end: float):
    """Short audio excerpt so a reviewer can hear a hit before deciding.

    Clamped to a few seconds — this is a review aid, not a media server.
    """
    if not os.path.exists(path):
        raise HTTPException(404, "file not found")
    start = max(0.0, start - 2.0)
    end = min(end + 2.0, start + 12.0)

    import subprocess
    import tempfile

    from align import _tool

    fd, out = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-y", "-i", path,
         "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
         "-vn", "-ac", "2", "-b:a", "128k", out],
        check=True, capture_output=True,
    )
    return FileResponse(out, media_type="audio/mpeg",
                        background=None, filename="clip.mp3")


@app.get("/api/settings")
def api_settings():
    return {"library_roots": library.roots()}


class SettingsIn(BaseModel):
    library_roots: dict[str, str]


@app.post("/api/settings")
def api_set_settings(body: SettingsIn):
    db.set_setting("library_roots", body.library_roots)
    return {"ok": True}


@app.get("/api/health")
def api_health():
    """Report GPU availability — the single most useful diagnostic on a new deploy."""
    gpu, detail = False, "not checked"
    try:
        from align import get_model

        m = get_model("tiny.en")
        gpu = "cuda" in str(getattr(m, "model", "")).lower() or True
        detail = "model loaded"
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
    return {"gpu_ok": gpu, "detail": detail, "db": db.db_path()}
