"""
Media library scanning and search.

Walks the configured roots, records what it finds, and probes audio parameters lazily —
probing every file on every browse would be unusably slow over SMB, so ffprobe results
are cached in the DB and only refreshed when a file's size or mtime changes.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from db import connect, get_setting, repath, tx  # noqa: E402

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts"}

#: Default library roots. Overridable via the `library_roots` setting so the same image
#: works on Unraid (/media/...) and during local development (Z:\...).
DEFAULT_ROOTS = {
    "movies": "/media/movies",
    "tv": "/media/tv",
    "kids_movies": "/media/kids_movies",
    "kids_tv": "/media/kids_tv",
    "toFilter": "/media/toFilter",
}

#: Filenames the app produces; never offer them as filter sources.
_SKIP_MARKERS = (".FILTERED.", ".ORIGINAL.", "unfilteredArchive")

#: Where originals go before filtering. `{root}` expands to the media root shared by the
#: configured libraries, so the default keeps archives on the same volume as the source —
#: which matters because a cross-volume archive is a full copy rather than a fast move.
DEFAULT_ARCHIVE = {
    "template": "{root}/toFilter/unfilteredArchive/{name}",
    "keep_tree": False,
}


def archive_config() -> dict:
    cfg = dict(DEFAULT_ARCHIVE)
    cfg.update(get_setting("archive", {}) or {})
    return cfg


def media_root() -> str:
    """Common parent of the configured library roots — the meaning of `{root}`.

    With roots like /media/tv and /media/movies this yields /media, so the default
    archive template keeps originals on the same volume as the source.

    Roots can span volumes (a dev machine might mix C:\\ and Z:\\), and a naive
    `commonpath` over those either raises or — worse — picks whichever volume happens to
    win, silently sending archives to the wrong disk. So group roots by volume and use
    the group holding the most libraries.
    """
    paths = [os.path.abspath(p) for p in roots().values() if p]
    if not paths:
        return ""
    if len(paths) == 1:
        return os.path.dirname(paths[0].rstrip("/\\")) or paths[0]

    by_drive: dict[str, list[str]] = {}
    for p in paths:
        by_drive.setdefault(os.path.splitdrive(p)[0].lower(), []).append(p)
    group = max(by_drive.values(), key=len)

    if len(group) == 1:
        return os.path.dirname(group[0].rstrip("/\\")) or group[0]
    try:
        return os.path.commonpath(group)
    except ValueError:
        return os.path.dirname(group[0].rstrip("/\\"))


def archive_path_for(src: str, cfg: dict | None = None) -> str:
    """Resolve the archive destination for one source file.

    Pass `cfg` to evaluate a candidate template without saving it (used by the settings
    preview), so previewing never mutates shared state.

    Template placeholders:
      {root}    the shared media root
      {name}    filename with extension
      {stem}    filename without extension
      {ext}     extension including the dot
      {library} which library the title belongs to
      {reldir}  the source's directory relative to its library root (with keep_tree)
    """
    cfg = cfg or archive_config()
    row = connect().execute(
        "SELECT library FROM titles WHERE path=?", (src,)
    ).fetchone()
    library = row["library"] if row else ""

    reldir = ""
    if cfg.get("keep_tree"):
        lib_root = roots().get(library)
        if lib_root:
            try:
                reldir = os.path.relpath(os.path.dirname(src), lib_root)
                if reldir == ".":
                    reldir = ""
            except ValueError:
                reldir = ""

    name = os.path.basename(src)
    stem, ext = os.path.splitext(name)
    out = cfg["template"].format(
        root=media_root(), name=name, stem=stem, ext=ext,
        library=library, reldir=reldir,
    )
    # Collapse the empty {reldir} case so the path has no doubled separators.
    return os.path.normpath(out)


def roots() -> dict[str, str]:
    """Library roots: DB setting wins, else the env seed, else the defaults.

    The env var lets a compose file or Unraid template seed sensible paths on first boot,
    while still letting the user change them in the UI afterwards.
    """
    stored = get_setting("library_roots")
    if stored:
        return stored
    env = os.environ.get("MOVIE_FILTER_ROOTS")
    if env:
        import json

        try:
            return json.loads(env)
        except ValueError:
            pass
    return DEFAULT_ROOTS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def within_roots(path: str) -> bool:
    """Is `path` inside a configured library root?

    Several endpoints take a filesystem path from the client (`/api/title`, `/api/clip`,
    `/api/runs`). Without this check the app would happily probe, stream, or overwrite any
    file the process can reach — which matters as soon as it is exposed beyond the LAN.
    """
    try:
        target = os.path.abspath(path)
        # realpath only resolves what exists; for a not-yet-created destination, resolve
        # the nearest existing ancestor so symlink tricks are still caught while an
        # archive directory that has not been made yet is not rejected.
        probe = target
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        if probe and os.path.exists(probe):
            target = os.path.join(os.path.realpath(probe),
                                  os.path.relpath(target, probe))
        target = os.path.normpath(target)
    except (OSError, ValueError):
        return False
    for root in roots().values():
        if not root:
            continue
        try:
            base = os.path.realpath(os.path.abspath(root))
        except OSError:
            continue
        try:
            if os.path.commonpath([target, base]) == base:
                return True
        except ValueError:
            continue        # different volumes have no common path
    return False


def is_candidate(path: str) -> bool:
    if os.path.splitext(path)[1].lower() not in VIDEO_EXTS:
        return False
    return not any(m in path for m in _SKIP_MARKERS)


def scan(progress=None) -> dict:
    """Walk all roots and upsert what we find. Returns counts per library.

    Cheap by design: `os.scandir` only, no ffprobe. Audio details are filled in on
    demand by `ensure_probed`.
    """
    counts: dict[str, int] = {}
    seen: list[str] = []

    for library, root in roots().items():
        if not os.path.isdir(root):
            counts[library] = 0
            continue
        n = 0
        for dirpath, dirnames, filenames in os.walk(root):
            # Don't descend into our own archive directories.
            dirnames[:] = [d for d in dirnames if d != "unfilteredArchive"]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if not is_candidate(full):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                _upsert(full, library, fn, st.st_size)
                seen.append(full)
                n += 1
                if progress and n % 50 == 0:
                    progress(library, n)
        counts[library] = n

    # Reconcile rows against what is actually on disk.
    #
    # A moved file looks like a deletion plus an unrelated new file, because every table
    # keys a title by its absolute path. Detect the move and carry the history across
    # before deleting anything, otherwise reorganising the library silently strands the
    # run list, the review decisions and the always-mute rules at a path nothing points
    # at any more.
    seen_set = set(seen)
    moved = _relink_moved(seen_set)

    # Drop rows for files that have disappeared, but keep any that were filtered so the
    # history of what we did survives a library reorganisation.
    with tx() as c:
        rows = c.execute(
            "SELECT path FROM titles WHERE status='unfiltered'"
        ).fetchall()
        gone = [r["path"] for r in rows if r["path"] not in seen_set]
        for p in gone:
            c.execute("DELETE FROM titles WHERE path=?", (p,))
    counts["_removed"] = len(gone)
    counts["_moved"] = len(moved)
    return counts


def _relink_moved(seen: set[str]) -> list[tuple[str, str]]:
    """Re-point titles whose file has moved. Returns the (old, new) pairs applied.

    A title is considered moved when a row's path is gone from disk and exactly one
    newly-seen path has the same filename and byte size. Both halves matter:

    * Name alone is not enough — `S01E01.mkv` occurs in every season directory, and
      matching on it would hand one episode's history to another.
    * Requiring a *unique* match is what makes this safe. Duplicate copies of the same
      file (the same rip in two libraries) produce two candidates, and guessing between
      them would attach the history to the wrong one. Ambiguous cases are left alone and
      handled by the manual "this title moved" action instead.

    Only titles with something worth keeping are considered. An unfiltered title has no
    history, so moving its row and deleting-then-recreating it are indistinguishable.
    """
    conn = connect()
    tracked = conn.execute(
        "SELECT path, name, size_bytes FROM titles WHERE status != 'unfiltered'"
    ).fetchall()
    missing = [r for r in tracked if r["path"] not in seen and not os.path.exists(r["path"])]
    if not missing:
        return []

    # Index candidate destinations by identity. Only a path with no history of its own
    # can receive a move — a filtered title at the new path is a different title that
    # happens to share a name and size, not the same file relocated.
    has_history = {
        r["path"] for r in conn.execute(
            "SELECT path FROM titles WHERE status != 'unfiltered'").fetchall()
    }
    fresh: dict[tuple[str, int], list[str]] = {}
    for p in seen:
        if p in has_history:
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        fresh.setdefault((os.path.basename(p), size), []).append(p)

    applied: list[tuple[str, str]] = []
    for r in missing:
        if r["size_bytes"] is None:
            continue
        cands = fresh.get((r["name"], r["size_bytes"]), [])
        # Exactly one candidate, and it must not already be a tracked title in its own
        # right (guarded above, but re-checked because one candidate list can serve
        # several missing rows).
        if len(cands) != 1:
            continue
        new = cands[0]
        if new == r["path"]:
            continue
        repath(r["path"], new)
        # `repath` replaces the row the scan just created, which is the one that knew
        # the file's *new* library and name. Re-derive them, or a title moved out of
        # toFilter keeps claiming to live there.
        scan_one(new)
        applied.append((r["path"], new))
        # Consume the candidate so two missing titles cannot both claim it.
        fresh[(r["name"], r["size_bytes"])] = []
    return applied


def _upsert(path: str, library: str, name: str, size: int) -> None:
    with tx() as c:
        c.execute(
            """
            INSERT INTO titles(path, library, name, size_bytes, seen_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                library=excluded.library,
                name=excluded.name,
                size_bytes=excluded.size_bytes,
                seen_at=excluded.seen_at
            """,
            (path, library, name, size, _now()),
        )


def move_candidates(old_path: str) -> list[dict]:
    """Where a missing title's file might have gone.

    The scan re-links only unambiguous moves. This lists the plausible destinations for
    the rest so the choice can be made by hand: same name and size first (near-certain,
    just not unique), then same size alone (renamed as well as moved).

    Only paths with no filtering history of their own are offered — a filtered title
    elsewhere is a different title, and moving onto it would destroy its record.
    """
    conn = connect()
    row = conn.execute(
        "SELECT name, size_bytes FROM titles WHERE path=?", (old_path,)).fetchone()
    if row is None or row["size_bytes"] is None:
        return []
    size = row["size_bytes"]

    taken = {
        r["path"] for r in conn.execute(
            "SELECT path FROM titles WHERE status != 'unfiltered'").fetchall()
    }
    out: list[dict] = []
    for r in conn.execute(
        "SELECT path, name, library, size_bytes FROM titles WHERE size_bytes=?", (size,)
    ).fetchall():
        p = r["path"]
        if p == old_path or p in taken or not os.path.exists(p):
            continue
        out.append({"path": p, "name": r["name"], "library": r["library"],
                    "same_name": r["name"] == row["name"]})
    # Exact-name matches are the confident ones; show them first.
    out.sort(key=lambda x: (not x["same_name"], x["path"]))
    return out


def library_of(path: str) -> str | None:
    """Which configured root contains `path`, if any."""
    try:
        target = os.path.realpath(os.path.abspath(path))
    except OSError:
        return None
    for name, root in roots().items():
        if not root:
            continue
        try:
            base = os.path.realpath(os.path.abspath(root))
            if os.path.commonpath([target, base]) == base:
                return name
        except (OSError, ValueError):
            continue
    return None


def scan_one(path: str) -> bool:
    """Refresh one title's row from the file on disk.

    Used after a move: the row carries the history from the old location, so its
    `library` and `name` still describe where the file used to be. Returns False if the
    path is not a library candidate.
    """
    if not is_candidate(path) or not os.path.exists(path):
        return False
    lib = library_of(path)
    if lib is None:
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    _upsert(path, lib, os.path.basename(path), size)
    return True


def ensure_probed(path: str, force: bool = False) -> dict:
    """Fill in duration and audio parameters for one title, caching the result."""
    conn = connect()
    row = conn.execute("SELECT * FROM titles WHERE path=?", (path,)).fetchone()
    if row is None:
        raise KeyError(path)
    if row["audio_codec"] and not force:
        return dict(row)

    try:
        import splice

        info = splice.probe(path)
        from align import probe_duration

        dur = probe_duration(path)
    except Exception as exc:  # noqa: BLE001 - a broken file shouldn't kill the browse
        with tx() as c:
            c.execute("UPDATE titles SET audio_codec=? WHERE path=?",
                      (f"error: {type(exc).__name__}", path))
        return dict(conn.execute("SELECT * FROM titles WHERE path=?", (path,)).fetchone())

    with tx() as c:
        c.execute(
            """UPDATE titles SET duration=?, audio_codec=?, audio_profile=?, channels=?
               WHERE path=?""",
            (dur, info.codec, info.profile, info.channels, path),
        )
    return dict(conn.execute("SELECT * FROM titles WHERE path=?", (path,)).fetchone())


def search(q: str = "", library: str | None = None, status: str | None = None,
           limit: int = 200, offset: int = 0) -> list[dict]:
    """Title search. Matches on name fragments, case-insensitively."""
    # Left-join the auto-fetch cache so the library view can show "no filters exist"
    # as a settled answer rather than an un-tried lookup.
    sql = ["""SELECT t.*, a.status AS autofetch_status, a.detail AS autofetch_detail,
                     a.score AS autofetch_score
              FROM titles t LEFT JOIN autofetch a ON a.path = t.path
              WHERE 1=1"""]
    args: list = []
    # Columns must be table-qualified: `status`, `name` and `library` all exist on both
    # `titles` and `autofetch`, and an unqualified reference is ambiguous.
    for term in (t for t in q.split() if t):
        sql.append("AND t.name LIKE ?")
        args.append(f"%{term}%")
    if library:
        sql.append("AND t.library=?")
        args.append(library)
    if status:
        sql.append("AND t.status=?")
        args.append(status)
    sql.append("ORDER BY t.library, t.name LIMIT ? OFFSET ?")
    args += [limit, offset]

    rows = connect().execute(" ".join(sql), args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["has_tagset"] = bool(r["tag_set_id"])
        d["size_gb"] = round((r["size_bytes"] or 0) / 1024**3, 2)
        out.append(d)
    return out


def stats() -> dict:
    c = connect()
    total = c.execute("SELECT COUNT(*) n FROM titles").fetchone()["n"]
    by_status = {
        r["status"]: r["n"] for r in
        c.execute("SELECT status, COUNT(*) n FROM titles GROUP BY status").fetchall()
    }
    by_lib = {
        r["library"]: r["n"] for r in
        c.execute("SELECT library, COUNT(*) n FROM titles GROUP BY library").fetchall()
    }
    return {"total": total, "by_status": by_status, "by_library": by_lib}
