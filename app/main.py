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
from typing import Literal

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
    # Resolve runs left mid-flight by a previous process before starting the worker:
    # interrupted ones are failed (nothing resumes them), still-queued ones go back on
    # the queue. Without this the first group shows as active forever and the second
    # would be silently discarded.
    failed, requeued = jobs.reap_orphans()
    if failed:
        print(f"[startup] marked {failed} interrupted run(s) as failed", flush=True)
    if requeued:
        print(f"[startup] re-queued {requeued} run(s) that had not started", flush=True)
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


class MoveIn(BaseModel):
    old_path: str
    new_path: str


@app.post("/api/library/moved")
def api_moved(body: MoveIn):
    """Re-point a title's history after its file was moved.

    The library scan detects unambiguous moves on its own; this covers the rest — a file
    that was renamed as well as moved, or one of several identical copies, where guessing
    would risk attaching the history to the wrong title.
    """
    old, new = body.old_path, body.new_path
    if old == new:
        raise HTTPException(400, "the two paths are the same")
    if not library.within_roots(new):
        raise HTTPException(403, "the new path is outside the configured library roots")
    if not os.path.exists(new):
        raise HTTPException(404, f"nothing at {new}")

    conn = db.connect()
    src = conn.execute("SELECT status FROM titles WHERE path=?", (old,)).fetchone()
    if not src:
        raise HTTPException(404, f"no title recorded at {old}")

    # Refuse to overwrite a title that has a history of its own — that is a different
    # title, not this one relocated, and the move would destroy its runs and decisions.
    dst = conn.execute("SELECT status FROM titles WHERE path=?", (new,)).fetchone()
    if dst and dst["status"] != "unfiltered":
        raise HTTPException(
            409,
            f"{new} is already a filtered title with its own history; moving onto it "
            f"would destroy that record")

    moved = db.repath(old, new)
    # The destination row was created by a scan and carries the file's real name and
    # library; re-derive them so the title does not keep the old location's labels.
    library.scan_one(new)
    return {"ok": True, "moved": moved}


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

    # The cleaned-up title, so the UI can seed external searches (VideoSkip, etc.) without
    # duplicating the release-filename parsing in JS.
    import titleparse

    parsed = titleparse.parse(info.get("name") or "")
    info["parsed_title"] = parsed.title
    info["parsed_year"] = parsed.year
    info["parsed_season"] = parsed.season
    info["parsed_episode"] = parsed.episode

    # Which cached tag-sets to offer for this title.
    #
    # An explicitly linked tag-set (`titles.tag_set_id`) is ALWAYS offered and listed
    # first. Name-overlap matching alone was hiding exactly the tag-sets the user had
    # picked by hand: they pick manually *because* the filename does not resemble the
    # catalogue title, so the same mismatch that forced the manual pick then filtered the
    # result out of this list.
    rows = db.connect().execute(
        "SELECT tag_set_id, work_id, title_hint, runtime FROM tagsets"
    ).fetchall()
    name = (info.get("name") or "").lower()
    linked_id = info.get("tag_set_id")
    matches = []
    for r in rows:
        is_linked = linked_id is not None and r["tag_set_id"] == linked_id
        if not is_linked and r["title_hint"] and not _looks_like(r["title_hint"], name):
            continue
        d = dict(r)
        d["linked"] = is_linked
        # Pre-flight the wrong-master problem: a tag-set keyed to a different cut has
        # every timing offset, and no amount of per-word precision fixes that. Historical
        # cases in this library ran +11s and +12s. Surfacing the delta here means the user
        # sees it before spending GPU time on a run that cannot land correctly.
        dur = info.get("duration")
        if dur and r["runtime"]:
            delta = dur - r["runtime"]
            d["runtime_delta"] = round(delta, 1)
            d["same_cut"] = abs(delta) <= 10.0
        matches.append(d)
    # Linked first, then closest runtime, so the dialog's default is the best guess.
    matches.sort(key=lambda d: (not d.get("linked"),
                                abs(d.get("runtime_delta") or 9e9)))
    info["tagsets"] = matches
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


class VidAngelLoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/vidangel/login")
def api_va_login(body: VidAngelLoginIn):
    """Log in to VidAngel and store the returned token.

    Saves a trip through DevTools. The password is used for this single request and
    **never stored** — only the token is kept, which is what every other call needs.
    Storing the password would leave a standing credential for the whole VidAngel
    account in a SQLite file, to avoid a login needed roughly once a year.
    """
    if not body.username.strip() or not body.password:
        raise HTTPException(400, "username and password are both required")
    try:
        token = vac.login(body.username.strip(), body.password)
    except vac.FetchError as exc:
        raise HTTPException(502, str(exc))

    db.set_setting("vidangel_token", token)
    return api_va_auth()


@app.delete("/api/vidangel/auth")
def api_va_clear_auth():
    db.set_setting("vidangel_token", None)
    return {"ok": True}


class AutoFetchIn(BaseModel):
    path: str | None = None          # one title; omit to sweep
    library: str | None = None       # limit a sweep to one library
    limit: int = 200
    auto: bool = True                # False = suggest only, never fetch
    force: bool = False              # ignore cached answers
    only_missing: bool = True        # skip titles that already have a tag-set


@app.post("/api/vidangel/autofetch")
def api_autofetch(body: AutoFetchIn):
    """Find and cache VidAngel filters automatically.

    One title runs inline; a sweep runs in the background, since matching thousands of
    files means thousands of throttled API calls.
    """
    import autofetch as af

    if not db.get_setting("vidangel_token"):
        raise HTTPException(400, "no VidAngel token saved")

    if body.path:
        row = db.connect().execute(
            "SELECT name, duration FROM titles WHERE path=?", (body.path,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown title")
        res = af.match_one(body.path, row["name"], row["duration"],
                           auto=body.auto, force=body.force)
        return {
            "status": res.status, "detail": res.detail, "score": res.score,
            "work_id": res.work_id, "tag_set_id": res.tag_set_id,
            "parsed": res.parsed, "candidates": res.candidates,
        }

    sql = ["SELECT path, name, duration FROM titles WHERE 1=1"]
    args: list = []
    if body.library:
        sql.append("AND library=?")
        args.append(body.library)
    if body.only_missing:
        sql.append("AND tag_set_id IS NULL")
    sql.append("ORDER BY library, name LIMIT ?")
    args.append(body.limit)
    rows = db.connect().execute(" ".join(sql), args).fetchall()
    targets = [(r["path"], r["name"], r["duration"]) for r in rows]
    if not targets:
        return {"queued": 0, "detail": "nothing to match"}

    _autofetch_state.update(total=len(targets), done=0, running=True,
                            counts={}, last="")

    def _worker():
        def progress(i, n, res):
            _autofetch_state["done"] = i
            _autofetch_state["last"] = f"{res.status}: {res.parsed}"
            _autofetch_state["counts"][res.status] = (
                _autofetch_state["counts"].get(res.status, 0) + 1)

        try:
            af.sweep(targets, auto=body.auto, progress=progress)
        finally:
            _autofetch_state["running"] = False

    import threading

    threading.Thread(target=_worker, daemon=True, name="autofetch").start()
    return {"queued": len(targets)}


_autofetch_state: dict = {"running": False, "total": 0, "done": 0,
                          "counts": {}, "last": ""}


@app.get("/api/vidangel/autofetch")
def api_autofetch_status():
    rows = db.connect().execute(
        "SELECT status, COUNT(*) n FROM autofetch GROUP BY status").fetchall()
    return {**_autofetch_state,
            "totals": {r["status"]: r["n"] for r in rows}}


@app.get("/api/vidangel/candidates")
def api_candidates(path: str, q: str | None = None):
    """Search results for one title, scored against its filename, for manual picking.

    `q` overrides the parsed title, since release naming often differs from the
    catalogue's ("The.X.Files.I.Want.to.Believe" vs "The X-Files: I Want to Believe").
    """
    import autofetch as af

    row = db.connect().execute(
        "SELECT name FROM titles WHERE path=?", (path,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown title")
    try:
        return af.candidates_for(path, row["name"], q)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except vac.FetchError as exc:
        raise HTTPException(502, str(exc))


class PickIn(BaseModel):
    path: str
    work_id: int | None = None
    kind: str = ""
    tag_set_id: int | None = None     # skip resolution when a specific one is chosen


@app.post("/api/vidangel/pick")
def api_pick(body: PickIn):
    """Attach a user-chosen VidAngel work (or tag-set) to a title.

    Bypasses the automatic score threshold: the user has seen the candidates and decided,
    so their choice wins.
    """
    import autofetch as af

    if body.work_id is None and body.tag_set_id is None:
        raise HTTPException(400, "provide work_id or tag_set_id")
    row = db.connect().execute(
        "SELECT name, duration FROM titles WHERE path=?", (body.path,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown title")

    res = af.fetch_for_work(body.path, row["name"], body.work_id or 0,
                            kind=body.kind, duration=row["duration"],
                            tag_set_id=body.tag_set_id)
    if res.status == "error":
        raise HTTPException(502, res.detail)
    return {"status": res.status, "detail": res.detail,
            "tag_set_id": res.tag_set_id, "work_id": res.work_id}


@app.get("/api/vidangel/suggestions")
def api_suggestions(limit: int = 100):
    """Titles whose best match scored too low to fetch automatically."""
    rows = db.connect().execute(
        """SELECT a.path, a.status, a.detail, a.score, a.work_id, t.name
           FROM autofetch a JOIN titles t ON t.path = a.path
           WHERE a.status IN ('suggested','none','error')
           ORDER BY a.score DESC LIMIT ?""", (limit,)
    ).fetchall()
    return {"suggestions": [dict(r) for r in rows]}


@app.get("/api/vidangel/search")
def api_va_search(q: str):
    """Search VidAngel by title.

    Returns filterable matches ranked by relevance, plus unfilterable ones flagged so the
    user knows VidAngel simply does not have that title rather than wondering.

    `work_id` is not a `tag_set_id`: fetching still needs the tag-set id, which no
    discovered endpoint maps from a work. The search makes finding a title easy and
    confirms it is filterable; getting its tag-set id is still manual.
    """
    token = db.get_setting("vidangel_token")
    if not token:
        raise HTTPException(400, "no VidAngel token saved")
    try:
        hits = vac.search(q, token)
    except vac.FetchError as exc:
        raise HTTPException(502, str(exc))

    hits.sort(key=lambda h: (-vac.relevance(h, q), not h.filterable, h.title))
    known = {
        r["work_id"]: r["tag_set_id"] for r in db.connect().execute(
            "SELECT work_id, tag_set_id FROM tagsets WHERE work_id IS NOT NULL"
        ).fetchall()
    }
    return {"results": [
        {"work_id": h.work_id, "title": h.title, "year": h.year, "kind": h.kind,
         "slug": h.slug, "tag_count": h.tag_count, "filterable": h.filterable,
         "reason": h.reason, "relevance": vac.relevance(h, q),
         "cached_tag_set_id": known.get(h.work_id)}
        for h in hits if vac.relevance(h, q) > 0 or not h.filterable
    ][:25]}


@app.get("/api/vidangel/resolve")
def api_va_resolve(work_id: int, kind: str = "", q: str = ""):
    """Resolve a work id to its tag-set ids — the link search alone cannot provide.

    Movies return one entry; shows return every episode. A title can have **several
    tag-sets**, one per streaming service, because services carry different cuts — The
    Godfather has two. `runtime` is included so the caller can pick the one matching the
    local file rather than guessing.
    """
    token = db.get_setting("vidangel_token")
    if not token:
        raise HTTPException(400, "no VidAngel token saved")
    try:
        entries = vac.resolve_tagsets(work_id, token, kind=kind)
    except vac.FetchError as exc:
        raise HTTPException(502, str(exc))

    cached = {
        r["tag_set_id"] for r in db.connect().execute(
            "SELECT tag_set_id FROM tagsets").fetchall()
    }
    out = []
    for e in entries:
        out.append({
            "work_id": e.work_id, "label": e.label, "title": e.title,
            "season": e.season, "episode": e.episode,
            "runtime": e.runtime, "tag_count": e.tag_count,
            "tag_sets": [
                {"tag_set_id": o.tag_set_id, "service": o.service, "type": o.kind,
                 "format": o.max_format, "cached": o.tag_set_id in cached}
                for o in e.offerings
            ],
            "tag_set_ids": e.tag_set_ids,
        })
    # Narrow a long episode list when the caller passed a filename-ish hint.
    if q:
        import re as _re

        m = _re.search(r"s(\d{1,2})\s*e(\d{1,2})", q, _re.I)
        if m:
            s_no, e_no = int(m.group(1)), int(m.group(2))
            narrowed = [x for x in out
                        if x["season"] == s_no and x["episode"] == e_no]
            if narrowed:
                out = narrowed
    return {"entries": out}


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


class SkipFileIn(BaseModel):
    payload: str | None = None
    url: str | None = None
    title_hint: str | None = None
    token: str | None = None


@app.post("/api/skipfiles")
def api_add_skipfile(body: SkipFileIn):
    """Store a VideoSkip/EDL/JSON filter file, pasted or fetched by URL.

    Pasting always works. Fetching needs outbound access and is unverified against the
    live Exchange, so it reports exactly what failed rather than a generic error.
    """
    import videoskip_client as vsc

    text, source = body.payload, "pasted"
    if not text:
        if not body.url:
            raise HTTPException(400, "provide either payload or url")
        try:
            text = vsc.fetch(body.url, body.token)
            source = body.url
        except vsc.FetchError as exc:
            raise HTTPException(502, str(exc))

    try:
        sk = vsc.parse_any(text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not sk.entries:
        raise HTTPException(400, "parsed the file but found no usable entries")

    fmt = ("json" if text.strip()[:1] in "{[" else
           "vsk" if "-->" in text else "edl")
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO skipfiles(title_hint, source, format, audio_count,
                                     video_count, payload, added_at)
               VALUES (?,?,?,?,?,?,datetime('now'))""",
            (body.title_hint or sk.title or None, source, fmt,
             len(sk.audio()), len(sk.video()), text),
        )
        new_id = cur.lastrowid
    return {"id": new_id, "format": fmt, "title": sk.title,
            "audio": len(sk.audio()), "video": len(sk.video()),
            "entries": len(sk.entries)}


@app.get("/api/skipfiles")
def api_list_skipfiles():
    rows = db.connect().execute(
        "SELECT id,title_hint,source,format,audio_count,video_count,added_at "
        "FROM skipfiles ORDER BY id DESC"
    ).fetchall()
    return {"skipfiles": [dict(r) for r in rows]}


@app.get("/api/skipfiles/{sid}")
def api_skipfile(sid: int):
    import videoskip_client as vsc

    row = db.connect().execute(
        "SELECT * FROM skipfiles WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown filter file")
    sk = vsc.parse_any(row["payload"])
    return {
        "id": sid, "title_hint": row["title_hint"], "format": row["format"],
        "entries": [
            {"start": e.start, "end": e.end, "category": e.category,
             "description": e.description, "kind": e.kind,
             "duration": round(e.duration, 3)}
            for e in sk.entries
        ],
    }


@app.delete("/api/skipfiles/{sid}")
def api_del_skipfile(sid: int):
    with db.tx() as c:
        c.execute("DELETE FROM skipfiles WHERE id=?", (sid,))
    return {"ok": True}


@app.get("/api/nudity/classes")
def api_nudity_classes():
    """Which NudeNet classes are actionable, and which are deliberately ignored."""
    import nudity as nud

    return {"actionable": list(nud.DEFAULT_CLASSES),
            "ignored": list(nud.BENIGN_CLASSES),
            "min_score": nud.MIN_SCORE}


@app.get("/api/frame")
def api_frame(path: str, at: float):
    """A single JPEG frame, so a nudity detection can be reviewed visually."""
    if not library.within_roots(path):
        raise HTTPException(403, "path is outside the configured library roots")
    if not os.path.exists(path):
        raise HTTPException(404, "file not found")

    import subprocess
    import tempfile

    from align import _tool

    fd, out = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{max(0.0, at):.3f}",
         "-i", path, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", out],
        check=True, capture_output=True,
    )
    return FileResponse(out, media_type="image/jpeg", filename="frame.jpg")


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
    #: Individual incidents chosen by ref_id. When present these take precedence over
    #: whole-category selection, so a user can take one scene from a category and leave
    #: its siblings alone.
    audio_refs: list[str] = []
    video_refs: list[str] = []
    #: Skip Whisper verification and mute the tag's own timestamps as given. Useful for
    #: incidents describing a conversation rather than a word, where there is nothing for
    #: Whisper to find, and as an escape hatch when a word genuinely is not located.
    trust_timestamps: bool = False
    words: list[str] | None = None
    #: Words to mute on sight, with no review step. Every scan hit for these is muted
    #: directly rather than joining the pending-review queue — "just filter out any
    #: 'shit' and 'fuck'" for a title where you already know the answer. Distinct from
    #: `words` (which decides what is *searched for*) and from the per-title always-mute
    #: rules (which persist across runs); this applies to this run only.
    #:
    #: Empty means review everything, which stays the default: a classifier's opinion is
    #: not auto-applied unless the user asks for it.
    auto_mute_words: list[str] = []
    manual_mutes: list[ManualMute] = []
    manual_cuts: list[ManualCut] = []
    videoskip_id: int | None = None
    detect_nudity: bool = False
    nudity_fps: float = 1.0
    nudity_min_score: float = 0.35
    nudity_min_hits: int = 2
    verify_nudity: bool = True
    #: Limit the nudity scan to one span of the film. Detection is the slow part of a run
    #: (a full pass costs minutes), so scanning only the reel you care about is the
    #: difference between a targeted re-run and re-scanning the whole title. None on both
    #: means scan everything.
    nudity_start: float | None = None
    nudity_end: float | None = None
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

    ns, ne = body.nudity_start, body.nudity_end
    if (ns is not None or ne is not None) and not body.detect_nudity:
        raise HTTPException(400, "nudity_start/nudity_end need nudity detection enabled")
    if ns is not None and ns < 0:
        raise HTTPException(400, "nudity_start cannot be negative")
    if ns is not None and ne is not None and ne <= ns:
        raise HTTPException(400, f"nudity window end must be after start ({ns}-{ne})")

    opts = body.model_dump()
    if opts["words"] is None:
        opts["words"] = db.enabled_words()

    if not (opts["categories"] or opts["video_categories"] or opts["audio_refs"]
            or opts["video_refs"] or opts["manual_mutes"]
            or opts["manual_cuts"] or opts["videoskip_id"] or opts["detect_nudity"]
            or (opts["do_scan"] and opts["words"])):
        raise HTTPException(400, "nothing to filter: pick categories, add a manual "
                                 "mute/cut, choose a VideoSkip filter, enable nudity "
                                 "detection, or enable the word-list scan")

    # Default output/archive paths: the filtered file takes the source's own name, so the
    # library keeps exactly one file per title and Plex has nothing to disambiguate. The
    # original survives only in unfilteredArchive, which is why the archive is mandatory
    # and the swap in jobs.py refuses to run without a verified one.
    #
    # This used to default to `{stem}.FILTERED{ext}`, which left the unfiltered original
    # sitting beside the filtered copy and both visible to Plex. An explicit output_path
    # is still honoured as-is — a caller asking for a separate file gets one.
    if not opts["output_path"]:
        opts["output_path"] = body.path
    if not opts["archive_path"]:
        opts["archive_path"] = library.archive_path_for(body.path)

    run_id = jobs.enqueue(body.path, opts)
    return {"run_id": run_id}


def _legacy_output_path(path: str) -> str:
    """What `output_path` used to default to, before the filtered file replaced the
    library copy in place."""
    stem, ext = os.path.splitext(path)
    return f"{stem}.FILTERED{ext}"


def _without_legacy_output(path: str, opts: dict) -> dict:
    """Drop a stored `output_path` that was never anything but the old default.

    `api_run` resolves the default and saves the *resolved* path into `options_json`, so
    every run recorded before the replace-in-place change carries `…FILTERED.mkv` around
    with it. Replaying those options verbatim — which is what a re-run and a prefilled
    dialog both do — reproduces exactly the two-files-per-title layout the new default
    exists to prevent, and it does so silently, because the stored path looks like a
    deliberate choice. Observed on Severance S01E01: a re-run launched after the fix was
    deployed still wrote a FILTERED sibling next to the original.

    Only the old default is recognised and cleared. A path the caller actually typed is
    left alone, even when it happens to end in `.FILTERED.` — a caller asking for a
    separate file still gets one, they just have to ask again after a re-run.
    """
    out = opts.get("output_path")
    if out and os.path.normpath(out) == os.path.normpath(_legacy_output_path(path)):
        opts = dict(opts)
        opts["output_path"] = None
    return opts


@app.get("/api/title/history")
def api_title_history(path: str):
    """Everything known about past filtering of one title.

    Runs are kept per title rather than only the latest, because a re-run after review
    produces a different result and the earlier one explains why. The archive path is
    included and existence-checked: it is the only route back to the raw cut, so knowing
    whether it is still there matters before re-filtering.
    """
    if not library.within_roots(path):
        raise HTTPException(403, "path is outside the configured library roots")

    row = db.connect().execute(
        """SELECT name, status, filtered_at, archive_path, tag_set_id, duration
           FROM titles WHERE path=?""", (path,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown title")

    runs = db.connect().execute(
        """SELECT id, status, stage, progress, created_at, started_at, finished_at,
                  error, options_json, report_json
           FROM runs WHERE path=? ORDER BY id DESC""", (path,)).fetchall()

    out_runs = []
    for r in runs:
        d = {k: r[k] for k in ("id", "status", "stage", "progress", "created_at",
                               "started_at", "finished_at", "error")}
        opts = json.loads(r["options_json"] or "{}")
        rep = json.loads(r["report_json"] or "{}") if r["report_json"] else {}
        inc = rep.get("incidents") or []
        d["summary"] = {
            "muted": len(rep.get("mutes") or []),
            "video_cuts": len(rep.get("video_ranges") or []),
            "verified": sum(1 for i in inc if str(i.get("status", "")).startswith("OK")),
            "review": sum(1 for i in inc if i.get("status") == "REVIEW"),
            "not_found": sum(1 for i in inc if i.get("status") == "NOT_FOUND"),
            "pending_review": len((rep.get("scan") or {}).get("pending_review") or []),
            "offset": (rep.get("offset") or {}).get("applied"),
            "render": (rep.get("render") or {}).get("summary"),
            "output": opts.get("output_path"),
        }
        d["options"] = {
            "quality": opts.get("quality"), "model": opts.get("model"),
            "categories": opts.get("categories"),
            "video_categories": opts.get("video_categories"),
            "tag_set_id": opts.get("tag_set_id"),
            "videoskip_id": opts.get("videoskip_id"),
            "do_scan": opts.get("do_scan"),
            "auto_mute_words": opts.get("auto_mute_words"),
            "detect_nudity": opts.get("detect_nudity"),
            "manual_mutes": len(opts.get("manual_mutes") or []),
            "manual_cuts": len(opts.get("manual_cuts") or []),
        }
        d["report"] = rep
        out_runs.append(d)

    archive = row["archive_path"]
    decisions = db.connect().execute(
        "SELECT at_time, word, action FROM decisions WHERE path=? ORDER BY at_time",
        (path,)).fetchall()
    word_rules = db.connect().execute(
        "SELECT word, action FROM word_rules WHERE path=? ORDER BY word",
        (path,)).fetchall()

    return {
        "path": path, "name": row["name"], "status": row["status"],
        "filtered_at": row["filtered_at"], "duration": row["duration"],
        "tag_set_id": row["tag_set_id"],
        "archive_path": archive,
        "archive_exists": bool(archive) and os.path.exists(archive),
        "runs": out_runs,
        "decisions": [dict(d) for d in decisions],
        "word_rules": [dict(r) for r in word_rules],
        # A title whose file is gone is either deleted or moved somewhere the scan could
        # not match unambiguously. Offering the candidates turns "my history vanished"
        # into one click.
        "file_exists": os.path.exists(path),
        "move_candidates": ([] if os.path.exists(path)
                            else library.move_candidates(path)),
    }


#: A running job with no heartbeat for this long is probably wedged. Chosen well above
#: the slowest normal gap: a full-episode Whisper scan and an x264 render both emit
#: nothing for minutes at a time, so anything tighter cries wolf.
STALL_SECONDS = 900


@app.get("/api/runs/live")
def api_runs_live(tail: int = 60):
    """Everything needed to watch active runs: stage, progress, log tail, staleness.

    `seconds_since_heartbeat` is the part that distinguishes stuck from slow — stage and
    percentage alone look identical either way.
    """
    from datetime import datetime, timezone

    rows = db.connect().execute(
        """SELECT id, path, status, stage, progress, log, error,
                  created_at, started_at, finished_at, heartbeat_at
           FROM runs
           WHERE status IN ('queued','running')
              OR finished_at >= datetime('now','-30 minutes')
           ORDER BY id DESC"""
    ).fetchall()

    now = datetime.now(timezone.utc)

    def age(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            parsed = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return round((now - parsed).total_seconds(), 1)

    # Position in line for the queued rows, so the live view can show and re-order them.
    order = jobs.queued_order()
    place = {rid: n for n, rid in enumerate(order, start=1)}

    out = []
    for r in rows:
        lines = (r["log"] or "").strip().splitlines()
        # Fall back through heartbeat -> started -> created. A run predating the heartbeat
        # column has none, and treating that as "not stale" reported a long-dead job as
        # healthy — the exact failure this endpoint exists to catch.
        since = age(r["heartbeat_at"]) or age(r["started_at"]) or age(r["created_at"])
        out.append({
            "id": r["id"],
            "name": os.path.basename(r["path"]),
            "path": r["path"],
            "status": r["status"],
            "stage": r["stage"],
            "progress": r["progress"] or 0,
            "created_at": r["created_at"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "elapsed": age(r["started_at"]) if r["status"] == "running" else None,
            "seconds_since_heartbeat": since,
            "stalled": bool(r["status"] == "running" and since
                            and since > STALL_SECONDS),
            "queue_position": place.get(r["id"]),
            "queue_length": len(order),
            "log_tail": lines[-tail:],
            "log_lines": len(lines),
            "error": (r["error"] or "").strip().splitlines()[-1:] or None,
        })

    return {
        "runs": out,
        "worker_alive": jobs.worker_alive(),
        "current": jobs.current(),
        "queued": sum(1 for r in out if r["status"] == "queued"),
        "running": sum(1 for r in out if r["status"] == "running"),
        "stall_seconds": STALL_SECONDS,
    }


@app.get("/api/runs/{run_id}/log")
def api_run_log(run_id: int, offset: int = 0):
    """Incremental log fetch, so polling sends only new lines rather than the whole log."""
    row = db.connect().execute(
        "SELECT log, status, stage, progress, heartbeat_at FROM runs WHERE id=?",
        (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown run")
    lines = (row["log"] or "").splitlines()
    return {"lines": lines[offset:], "total": len(lines),
            "status": row["status"], "stage": row["stage"],
            "progress": row["progress"] or 0}


@app.get("/api/runs")
def api_runs(limit: int = 50):
    rows = db.connect().execute(
        "SELECT id,path,status,stage,progress,created_at,finished_at,error "
        "FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    # Where each pending run sits in line. Sent as a 1-based position rather than the raw
    # queue_pos so the UI does not have to know that the column has gaps, and so "3 of 11"
    # is directly displayable.
    order = jobs.queued_order()
    place = {rid: n for n, rid in enumerate(order, start=1)}
    out = []
    for r in rows:
        d = dict(r)
        d["queue_position"] = place.get(r["id"])
        d["queue_length"] = len(order)
        out.append(d)
    return {"runs": out, "current": jobs.current(), "queue_order": order}


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


@app.get("/api/runs/{run_id}/options")
def api_run_options(run_id: int):
    """A run's full options, for reopening the filter dialog prefilled.

    Re-running unchanged is useless for a failed run — the four archive failures in
    testing would all have failed again identically. Editing before re-running is the
    point, so the raw options come back rather than a summary.
    """
    row = db.connect().execute(
        "SELECT path, options_json, status FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown run")
    opts = _without_legacy_output(row["path"], json.loads(row["options_json"] or "{}"))
    return {"path": row["path"], "status": row["status"], "options": opts}


@app.post("/api/runs/{run_id}/rerun")
def api_rerun(run_id: int):
    """Queue the same run again, reusing its options.

    The interactive review stores decisions but does not retro-apply them: a mute has to
    be located and rendered, which means another pass. This makes that one click instead
    of rebuilding the run by hand.

    The archive is skipped if one already exists — `render()` refuses to overwrite an
    archive, and the first run's copy is still the untouched original.

    A stored output path from before the replace-in-place change is dropped rather than
    replayed, so the re-run writes over the library copy like a fresh run would.
    """
    row = db.connect().execute(
        "SELECT path, options_json FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "unknown run")

    opts = _without_legacy_output(row["path"], json.loads(row["options_json"] or "{}"))
    if not opts.get("output_path"):
        opts["output_path"] = row["path"]
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
        # Clear the queue position along with the status: the pending queue is derived
        # from that column, and a cancelled run must drop out of it.
        c.execute("UPDATE runs SET status='cancelled', queue_pos=NULL WHERE id=?",
                  (run_id,))
    return {"ok": True}


class QueueMoveIn(BaseModel):
    #: 'front' jumps the whole queue; 'up'/'down' swap with the neighbour. Front exists
    #: because the case this is for is a job stuck behind ten others.
    to: Literal["up", "down", "front"]


@app.post("/api/runs/{run_id}/move")
def api_move(run_id: int, body: QueueMoveIn):
    """Re-prioritise a queued run.

    Only pending runs can move. The job already running is mid-write in ffmpeg or Whisper
    and cannot be displaced — the same reason it cannot be cancelled.
    """
    try:
        if body.to == "front":
            order = jobs.move_to_front(run_id)
        else:
            order = jobs.reorder(run_id, body.to)
    except LookupError:
        row = db.connect().execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown run") from None
        raise HTTPException(409, f"cannot re-order a {row['status']} run") from None
    return {"ok": True, "order": order, "position": order.index(run_id) + 1}


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
    _record([(body.at_time, body.word)], body.path, body.action, body.note)
    return {"ok": True}


class HitRef(BaseModel):
    at_time: float
    word: str


class BulkDecisionIn(BaseModel):
    path: str
    action: str                  # mute | skip
    hits: list[HitRef]
    note: str | None = None


def _record(hits, path: str, action: str, note: str | None) -> int:
    """Persist one decision per hit. Shared by the single and bulk endpoints."""
    with db.tx() as c:
        for at_time, word in hits:
            c.execute(
                "INSERT INTO decisions(path,at_time,word,action,note) VALUES (?,?,?,?,?) "
                "ON CONFLICT(path,at_time,word) DO UPDATE SET action=excluded.action, "
                "note=excluded.note",
                (path, round(at_time, 2), word, action, note),
            )
    return len(hits)


@app.post("/api/review/bulk")
def api_decide_bulk(body: BulkDecisionIn):
    """Decide a whole group of hits at once.

    Backs "mute all 23 f-words" and shift-click range selection. One decision row per
    hit rather than a rule, so individual hits stay individually revisable afterwards.
    """
    if body.action not in ("mute", "skip"):
        raise HTTPException(400, "action must be 'mute' or 'skip'")
    n = _record([(h.at_time, h.word) for h in body.hits], body.path, body.action,
                body.note)
    return {"ok": True, "count": n}


class WordRuleIn(BaseModel):
    path: str
    word: str
    action: str                  # mute | skip


@app.post("/api/review/word")
def api_word_rule(body: WordRuleIn):
    """Set a blanket rule: every instance of this word in this title, now and later.

    This is the durable form of "select all the F-words". A per-hit decision only covers
    the timestamps the scan happened to report on that pass; a rule covers the word
    however its timings move, so a later run never re-asks about it — including for
    instances no earlier scan had found.
    """
    if body.action not in ("mute", "skip"):
        raise HTTPException(400, "action must be 'mute' or 'skip'")
    word = db._norm_word(body.word)
    if not word:
        raise HTTPException(400, "word is required")
    with db.tx() as c:
        c.execute(
            "INSERT INTO word_rules(path,word,action,created_at) "
            "VALUES (?,?,?,datetime('now')) "
            "ON CONFLICT(path,word) DO UPDATE SET action=excluded.action",
            (body.path, word, body.action),
        )
    return {"ok": True, "word": word}


@app.delete("/api/review/word")
def api_word_rule_clear(path: str, word: str):
    """Drop a blanket rule so its word returns to per-instance review."""
    with db.tx() as c:
        c.execute("DELETE FROM word_rules WHERE path=? AND word=?",
                  (path, db._norm_word(word)))
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
    """Report GPU availability — the single most useful diagnostic on a new deploy.

    `get_model` already proves the GPU by running a real inference and silently falls
    back to CPU, so the only question left here is which device it settled on. That
    comes from the ctranslate2 model itself; anything else is a guess.
    """
    gpu, device, detail = False, "unknown", "not checked"
    try:
        from align import get_model

        m = get_model("tiny.en")
        device = str(getattr(getattr(m, "model", None), "device", "unknown")).lower()
        gpu = device.startswith("cuda")
        detail = "model loaded"
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
    return {"gpu_ok": gpu, "device": device, "detail": detail, "db": db.db_path()}
