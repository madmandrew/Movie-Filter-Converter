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

from db import connect, get_setting, tx  # noqa: E402

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

    # Drop rows for files that have disappeared, but keep any that were filtered so the
    # history of what we did survives a library reorganisation.
    with tx() as c:
        rows = c.execute(
            "SELECT path FROM titles WHERE status='unfiltered'"
        ).fetchall()
        gone = [r["path"] for r in rows if r["path"] not in set(seen)]
        for p in gone:
            c.execute("DELETE FROM titles WHERE path=?", (p,))
    counts["_removed"] = len(gone)
    return counts


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
    sql = ["SELECT * FROM titles WHERE 1=1"]
    args: list = []
    for term in (t for t in q.split() if t):
        sql.append("AND name LIKE ?")
        args.append(f"%{term}%")
    if library:
        sql.append("AND library=?")
        args.append(library)
    if status:
        sql.append("AND status=?")
        args.append(status)
    sql.append("ORDER BY library, name LIMIT ? OFFSET ?")
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
