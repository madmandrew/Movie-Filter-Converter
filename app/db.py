"""
SQLite persistence: library state, run history, the always-filter word list, and the
user's review decisions.

One file, `data/filter.db`, so backing up the whole thing is a copy. Schema is created
on first use and migrated forward by additive `ALTER`s, never destructively.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    path         TEXT PRIMARY KEY,
    library      TEXT NOT NULL,          -- movies | tv | kids_movies | kids_tv | toFilter
    name         TEXT NOT NULL,
    size_bytes   INTEGER,
    duration     REAL,
    audio_codec  TEXT,
    audio_profile TEXT,
    channels     INTEGER,
    status       TEXT NOT NULL DEFAULT 'unfiltered',  -- unfiltered|filtered|failed
    filtered_at  TEXT,
    archive_path TEXT,
    tag_set_id   INTEGER,
    report_json  TEXT,
    seen_at      TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL,
    status       TEXT NOT NULL,          -- queued|running|done|failed|cancelled
    stage        TEXT,
    progress     REAL DEFAULT 0,
    options_json TEXT,
    log          TEXT DEFAULT '',
    report_json  TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

-- Words always filtered regardless of whether VidAngel tagged them. The full-episode
-- scan uses this list, which is why a tag-set is optional rather than required.
CREATE TABLE IF NOT EXISTS wordlist (
    word     TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'profanity',
    enabled  INTEGER NOT NULL DEFAULT 1
);

-- Per-hit review decisions from the interactive scan review, keyed by title + time so
-- a re-run does not re-ask what the user already judged.
CREATE TABLE IF NOT EXISTS decisions (
    path     TEXT NOT NULL,
    at_time  REAL NOT NULL,
    word     TEXT NOT NULL,
    action   TEXT NOT NULL,             -- mute | skip
    note     TEXT,
    PRIMARY KEY (path, at_time, word)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Cached VidAngel payloads. Claude/the server cannot reach the API, so these are
-- uploaded by the user and reused.
-- VideoSkip Exchange / EDL filter files, pasted or downloaded. Second-choice source when
-- VidAngel has no tag-set for a title.
CREATE TABLE IF NOT EXISTS skipfiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title_hint  TEXT,
    source      TEXT,               -- url or 'pasted'
    format      TEXT,               -- vsk | edl | json
    audio_count INTEGER,
    video_count INTEGER,
    payload     TEXT NOT NULL,
    added_at    TEXT
);

CREATE TABLE IF NOT EXISTS tagsets (
    tag_set_id  INTEGER PRIMARY KEY,
    work_id     INTEGER,
    title_hint  TEXT,
    runtime     REAL,
    payload     TEXT NOT NULL,
    added_at    TEXT
);
"""

DEFAULT_WORDS = [
    ("damn", "profanity"), ("hell", "profanity"), ("ass", "profanity"),
    ("asshole", "profanity"), ("bitch", "profanity"), ("bastard", "profanity"),
    ("shit", "profanity"), ("bullshit", "profanity"), ("fuck", "profanity"),
    ("fucking", "profanity"), ("goddamn", "blasphemy"), ("god", "blasphemy"),
    ("jesus", "blasphemy"), ("christ", "blasphemy"), ("dick", "crude"),
    ("piss", "crude"), ("crap", "crude"), ("douche", "crude"),
]


def db_path() -> str:
    return os.environ.get("FILTER_DB", os.path.join("data", "filter.db"))


def connect() -> sqlite3.Connection:
    """Per-thread connection. The job worker and request handlers share the file."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        path = db_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL lets the worker write progress while requests read it.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _LOCAL.conn = conn
    return conn


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM wordlist LIMIT 1").fetchone():
        conn.executemany(
            "INSERT INTO wordlist(word, category, enabled) VALUES (?,?,1)",
            DEFAULT_WORDS,
        )
    conn.commit()


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_setting(key: str, default=None):
    row = connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def enabled_words() -> list[str]:
    rows = connect().execute(
        "SELECT word FROM wordlist WHERE enabled=1 ORDER BY word"
    ).fetchall()
    return [r["word"] for r in rows]
