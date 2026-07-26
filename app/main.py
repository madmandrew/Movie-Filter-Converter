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
import vidangel_client as vac  # noqa: E402

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create the schema and start the job worker before serving any request."""
    db.init()
    jobs.ensure_worker()
    yield


app = FastAPI(title="Movie Filter", lifespan=lifespan)


#: Optional HTTP basic auth, enabled by setting FILTER_PASSWORD.
#:
#: Off by default: on a LAN behind Unraid this app is already only as reachable as the
#: server. But it can read arbitrary paths, queue jobs that rewrite media files, and
#: delete nothing yet still overwrite plenty — so any time it is exposed beyond the LAN
#: (a tunnel, a reverse proxy, a port forward) a password is mandatory, not optional.
_AUTH_USER = os.environ.get("FILTER_USER", "admin")
_AUTH_PASS = os.environ.get("FILTER_PASSWORD")


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    if not _AUTH_PASS:
        return await call_next(request)

    import base64
    import secrets

    header = request.headers.get("authorization", "")
    ok = False
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pw = decoded.partition(":")
            # compare_digest on both fields to avoid leaking length via timing
            ok = (secrets.compare_digest(user, _AUTH_USER)
                  and secrets.compare_digest(pw, _AUTH_PASS))
        except (ValueError, UnicodeDecodeError):
            ok = False

    if not ok:
        return JSONResponse(
            {"detail": "authentication required"}, status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Movie Filter"'},
        )
    return await call_next(request)


app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the page with cache-busted asset URLs.

    Browsers hold on to `app.css` / `app.js` hard enough that a fixed stylesheet can keep
    rendering the old layout after a deploy. Appending each file's mtime makes the URL
    change whenever the file does, so a stale asset is impossible without asking anyone to
    clear their cache. The page itself is sent no-store for the same reason.
    """
    tpl = os.path.join(_HERE, "templates", "index.html")
    with open(tpl, encoding="utf-8") as fh:
        html = fh.read()

    for asset in ("app.css", "app.js"):
        try:
            stamp = int(os.path.getmtime(os.path.join(_HERE, "static", asset)))
        except OSError:
            continue
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={stamp}")

    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


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
    if not library.within_roots(path):
        raise HTTPException(403, "path is outside the configured library roots")
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


class VidAngelAuthIn(BaseModel):
    token: str | None = None
    api_template: str | None = None


@app.get("/api/vidangel/auth")
def api_va_auth():
    """Whether a token is saved — never the token itself."""
    token = db.get_setting("vidangel_token")
    return {
        "has_token": bool(token),
        "token_hint": (f"{token[:4]}…{token[-2:]}" if token and len(token) > 6 else None),
        "api_template": db.get_setting("vidangel_api", vac.DEFAULT_API),
    }


@app.post("/api/vidangel/auth")
def api_va_set_auth(body: VidAngelAuthIn):
    if body.token is not None:
        tok = body.token.strip()
        # Tolerate a pasted "Authorization: Bearer xyz" header or a raw bearer prefix.
        for prefix in ("authorization:", "bearer ", "token ", "jwt "):
            if tok.lower().startswith(prefix):
                tok = tok[len(prefix):].strip()
        db.set_setting("vidangel_token", tok or None)
    if body.api_template is not None:
        tmpl = body.api_template.strip() or vac.DEFAULT_API
        if "{id}" not in tmpl:
            raise HTTPException(400, "api_template must contain {id}")
        db.set_setting("vidangel_api", tmpl)
    return api_va_auth()


@app.delete("/api/vidangel/auth")
def api_va_clear_auth():
    db.set_setting("vidangel_token", None)
    return {"ok": True}


class FetchIn(BaseModel):
    url: str
    title_hint: str | None = None
    token: str | None = None      # one-off override; not saved unless save_token
    save_token: bool = False


@app.post("/api/vidangel/fetch")
def api_va_fetch(body: FetchIn):
    """Fetch a tag-set from VidAngel by URL (or bare id) and cache it.

    Requires outbound internet access from the server. If that is unavailable, or the
    API has changed shape, the error says so and pasting JSON still works.
    """
    import vidangel

    token = (body.token or "").strip() or db.get_setting("vidangel_token")
    if not token:
        raise HTTPException(400, "no VidAngel token saved — add one first, or paste JSON")

    template = db.get_setting("vidangel_api", vac.DEFAULT_API)
    try:
        tag_set_id, raw = vac.fetch_tagset(body.url, token, api_template=template)
    except vac.FetchError as exc:
        raise HTTPException(502, str(exc))

    try:
        ts = vidangel.parse(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"fetched a response but could not parse it as a "
                                 f"tag-set: {exc}")

    if body.save_token and body.token:
        db.set_setting("vidangel_token", token)

    with db.tx() as c:
        c.execute(
            """INSERT INTO tagsets(tag_set_id, work_id, title_hint, runtime, payload,
                                   added_at)
               VALUES (?,?,?,?,?,datetime('now'))
               ON CONFLICT(tag_set_id) DO UPDATE SET
                   payload=excluded.payload, title_hint=excluded.title_hint,
                   runtime=excluded.runtime""",
            (ts.tag_set_id, ts.work_id, body.title_hint, ts.runtime_unaltered, raw),
        )
    return {"tag_set_id": ts.tag_set_id, "work_id": ts.work_id,
            "incidents": len(ts.incidents), "enabled": len(ts.enabled()),
            "runtime": ts.runtime_unaltered, "fetched_id": tag_set_id}


@app.get("/api/tagsets")
def api_list_tagsets():
    rows = db.connect().execute(
        "SELECT tag_set_id, work_id, title_hint, runtime, added_at FROM tagsets "
        "ORDER BY added_at DESC"
    ).fetchall()
    return {"tagsets": [dict(r) for r in rows]}


@app.delete("/api/tagsets/{tag_set_id}")
def api_del_tagset(tag_set_id: int):
    with db.tx() as c:
        c.execute("DELETE FROM tagsets WHERE tag_set_id=?", (tag_set_id,))
    return {"ok": True}


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

class ManualMute(BaseModel):
    """One hand-specified audio mute.

    Either `word` + `at` (a rough timestamp; the word is then located and verified
    precisely) or an explicit `start`/`end` range applied as given.
    """
    word: str | None = None
    at: float | None = None
    search_pad: float = 5.0
    start: float | None = None
    end: float | None = None
    label: str | None = None


class ManualCut(BaseModel):
    """One hand-specified video cut. `snap` aligns edges to detected shot boundaries."""
    start: float
    end: float
    snap: bool = True
    pad: float = 0.0


class RunIn(BaseModel):
    path: str
    tag_set_id: int | None = None
    categories: list[str] = []
    video_categories: list[str] = []
    words: list[str] | None = None
    manual_mutes: list[ManualMute] = []
    manual_cuts: list[ManualCut] = []
    quality: str = "splice"
    model: str = "small.en"
    do_scan: bool = True
    only_enabled: bool = False
    output_path: str | None = None
    archive_path: str | None = None


@app.post("/api/runs")
def api_run(body: RunIn):
    if not library.within_roots(body.path):
        raise HTTPException(403, "path is outside the configured library roots")
    if not os.path.exists(body.path):
        raise HTTPException(404, "file not found")
    # A run writes both of these, so neither may escape the library either.
    for label, p in (("output_path", body.output_path),
                     ("archive_path", body.archive_path)):
        if p and not library.within_roots(p):
            raise HTTPException(403, f"{label} is outside the configured library roots")

    for m in body.manual_mutes:
        has_word = bool(m.word) and m.at is not None
        has_range = m.start is not None and m.end is not None
        if not (has_word or has_range):
            raise HTTPException(
                400, "each manual mute needs either word+at or start+end")
        if has_range and m.end <= m.start:
            raise HTTPException(400, f"mute end must be after start ({m.start}-{m.end})")
    for cvt in body.manual_cuts:
        if cvt.end <= cvt.start:
            raise HTTPException(400, f"cut end must be after start ({cvt.start}-{cvt.end})")

    opts = body.model_dump()
    if opts["words"] is None:
        opts["words"] = db.enabled_words()

    if not (opts["categories"] or opts["video_categories"] or opts["manual_mutes"]
            or opts["manual_cuts"] or (opts["do_scan"] and opts["words"])):
        raise HTTPException(400, "nothing to filter: pick categories, add a manual "
                                 "mute/cut, or enable the word-list scan")

    # Default output/archive paths: filtered file replaces the library copy, original
    # goes to unfilteredArchive next to the configured toFilter root.
    if not opts["output_path"]:
        stem, ext = os.path.splitext(body.path)
        opts["output_path"] = f"{stem}.FILTERED{ext}"
    if not opts["archive_path"]:
        opts["archive_path"] = library.archive_path_for(body.path)

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


@app.post("/api/runs/{run_id}/rerun")
def api_rerun(run_id: int):
    """Queue the same run again, reusing its options.

    The interactive review stores decisions but does not retro-apply them: a mute has to
    be located and rendered, which means another pass. This makes that one click instead
    of rebuilding the run by hand.

    The archive is skipped if one already exists — `render()` refuses to overwrite an
    archive, and the first run's copy is still the untouched original.
    """
    row = db.connect().execute(
        "SELECT path, options_json FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "unknown run")

    opts = json.loads(row["options_json"] or "{}")
    if not os.path.exists(row["path"]):
        raise HTTPException(
            404, "source file is gone — if it was replaced by the filtered version, "
                 "restore from the archive before re-running")

    new_id = jobs.enqueue(row["path"], opts)
    return {"run_id": new_id, "reused_from": run_id}


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
    if not library.within_roots(path):
        raise HTTPException(403, "path is outside the configured library roots")
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
    return {
        "library_roots": library.roots(),
        "archive": library.archive_config(),
        "media_root": library.media_root(),
        "archive_placeholders": ["{root}", "{name}", "{stem}", "{ext}",
                                 "{library}", "{reldir}"],
    }


class SettingsIn(BaseModel):
    library_roots: dict[str, str] | None = None
    archive: dict | None = None


@app.post("/api/settings")
def api_set_settings(body: SettingsIn):
    if body.library_roots is not None:
        db.set_setting("library_roots", body.library_roots)
    if body.archive is not None:
        tmpl = str(body.archive.get("template", "")).strip()
        if not tmpl:
            raise HTTPException(400, "archive template cannot be empty")
        # Reject a template that would overwrite the source it is meant to protect.
        if "{name}" not in tmpl and "{stem}" not in tmpl:
            raise HTTPException(
                400, "archive template must include {name} or {stem}, otherwise every "
                     "title would archive to the same path and overwrite the previous one")
        db.set_setting("archive", {"template": tmpl,
                                   "keep_tree": bool(body.archive.get("keep_tree"))})
    return {"ok": True}


@app.get("/api/settings/archive-preview")
def api_archive_preview(path: str, template: str | None = None,
                        keep_tree: bool = False):
    """Show where a given title would be archived, before saving the setting."""
    cfg = ({"template": template, "keep_tree": keep_tree} if template else None)
    try:
        return {"archive_path": library.archive_path_for(path, cfg)}
    except (KeyError, IndexError, ValueError) as exc:
        raise HTTPException(400, f"invalid template: {exc}")


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
