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
    finished_at  TEXT,
    -- When the stage/progress last moved. Without this a hung job is indistinguishable
    -- from a slow one: both show the same stage and percentage indefinitely.
    heartbeat_at TEXT
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
--
-- `at_time` is matched with a tolerance, never by equality: Whisper's word timestamps
-- move a few tens of milliseconds between decodes, so an exact key silently failed to
-- recognise a hit the user had already judged and re-asked about it every single run.
-- See `decision_for()`.
CREATE TABLE IF NOT EXISTS decisions (
    path     TEXT NOT NULL,
    at_time  REAL NOT NULL,
    word     TEXT NOT NULL,
    action   TEXT NOT NULL,             -- mute | skip
    note     TEXT,
    PRIMARY KEY (path, at_time, word)
);

-- Blanket per-word rules: "mute every instance of this word in this title". A rule
-- outranks nothing — it is consulted only where no per-hit decision exists — but it
-- means a word the user has already ruled on never comes back for review, however the
-- scan's timings shift. This is what makes "select all the F-words" a one-time action
-- instead of a per-instance chore repeated every pass.
CREATE TABLE IF NOT EXISTS word_rules (
    path       TEXT NOT NULL,
    word       TEXT NOT NULL,           -- normalised stem, matched via _variants()
    action     TEXT NOT NULL,           -- mute | skip
    created_at TEXT,
    PRIMARY KEY (path, word)
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

-- Auto-fetch results per title. Caches the negative answers too ("VidAngel has no
-- filters for this") so a library sweep does not re-query thousands of titles every run.
CREATE TABLE IF NOT EXISTS autofetch (
    path        TEXT PRIMARY KEY,
    status      TEXT NOT NULL,      -- fetched|suggested|none|unfilterable|error
    work_id     INTEGER,
    tag_set_id  INTEGER,
    score       INTEGER,
    detail      TEXT,
    checked_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_autofetch_status ON autofetch(status);

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


#: Columns added after the initial schema. `CREATE TABLE IF NOT EXISTS` is a no-op on an
#: existing table, so new columns need an explicit ALTER — additive only, never
#: destructive, so an older database keeps working.
_MIGRATIONS = (
    ("runs", "heartbeat_at", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, coltype in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
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


#: Every table that keys a title by its absolute path. A file moving on disk must
#: re-point all of them together or the history silently detaches from the title: the
#: run list, the review decisions and the always-mute rules would all still exist but
#: be unreachable from the new path.
_PATH_KEYED = ("titles", "runs", "decisions", "word_rules", "autofetch")


def repath(old: str, new: str) -> dict[str, int]:
    """Move a title's entire history from `old` to `new`. Returns rows touched per table.

    Used when a file is moved on disk. Everything about a title — its runs, the words the
    user judged, the always-mute rules — is keyed on the absolute path, so without this a
    reorganisation looks like the old title vanishing and an unrelated new one appearing.

    A row already existing at `new` (the scan will have inserted a bare `titles` row for
    the file in its new location) is replaced by the one carrying the history.
    """
    if old == new:
        return {}
    moved: dict[str, int] = {}
    with tx() as c:
        for table in _PATH_KEYED:
            # Clear the destination first: `titles.path` is a primary key and
            # `decisions`/`word_rules` have composite keys including it, so an UPDATE
            # onto an occupied key would fail the whole move. The row being displaced is
            # the historyless one the scan just created.
            c.execute(f"DELETE FROM {table} WHERE path=?", (new,))
            cur = c.execute(f"UPDATE {table} SET path=? WHERE path=?", (new, old))
            if cur.rowcount:
                moved[table] = cur.rowcount
    return moved


#: How far a stored decision's timestamp may sit from a scan hit and still be considered
#: the same word. Whisper re-decodes the same audio to slightly different boundaries run
#: to run (documented in CLAUDE.md), so matching on equality loses the decision entirely.
#: 0.5s is comfortably wider than the observed jitter and narrower than the gap between
#: two distinct utterances of the same word in rapid speech.
DECISION_TOL = 0.5


class Decisions:
    """Resolved review state for one title: per-hit decisions plus per-word rules.

    Loaded once per run rather than queried per hit — a scan produces hundreds of hits
    and the decision set is small.
    """

    def __init__(self, per_hit: list[tuple[float, str, str]],
                 rules: dict[str, str]) -> None:
        #: (at_time, word, action), sorted by time so lookup can stop early.
        self._hits = sorted(per_hit)
        self._rules = rules

    def rule_for(self, word: str) -> str | None:
        """The blanket action for `word`, if the user set one.

        A rule is stored against the stem the user reviewed, but the scan reports the
        inflection it actually heard, so a rule on "fuck" must also answer for "fucking"
        and "fucks". Matching goes through the same variant expansion the word matcher
        uses, so the two never disagree about what counts as the same word.
        """
        norm = _norm_word(word)
        if norm in self._rules:
            return self._rules[norm]
        for stem, action in self._rules.items():
            if norm in _word_variants(stem):
                return action
        return None

    def muted_words(self) -> set[str]:
        """Words under a blanket mute rule.

        The scan must look for these even when they are not on the run's word list —
        a rule is meant to catch instances no earlier pass found, and an unsearched word
        is never found.
        """
        return {w for w, a in self._rules.items() if a == "mute"}

    def action_for(self, word: str, at: float) -> str | None:
        """What the user already decided about this word at this time, if anything.

        A per-hit decision wins where one exists — it is the more specific instruction,
        so "mute every f-word except this one line" stays expressible. Otherwise the
        per-word rule applies.
        """
        norm = _norm_word(word)
        best, best_gap = None, DECISION_TOL
        for t, w, action in self._hits:
            if t - at > DECISION_TOL:
                break
            gap = abs(t - at)
            if gap <= best_gap and _norm_word(w) == norm:
                best, best_gap = action, gap
        if best is not None:
            return best
        return self.rule_for(word)


def _word_variants(stem: str) -> set[str]:
    """Inflections of `stem` that count as the same word.

    Delegates to the pipeline's own matcher so a rule covers exactly the forms the scan
    would have flagged. Falls back to the bare stem if `tools/` is not importable (the
    web process adds it to sys.path, but db.py is also imported by tooling that may not).
    """
    try:
        from locate import _variants
    except ImportError:
        return {stem}
    return _variants(stem)


def _norm_word(w: str) -> str:
    """Fold a word to the form decisions are keyed on.

    Uses the same normalisation as the matcher so "Fucking" and "fucking" — and a rule
    on "fuck" against a hit of "fucking" — resolve together rather than being treated as
    unrelated words.
    """
    import re
    return re.sub(r"[^a-z]", "", (w or "").lower())


def load_decisions(path: str) -> Decisions:
    """Every review decision and word rule recorded for one title."""
    conn = connect()
    per_hit = [
        (r["at_time"], r["word"], r["action"])
        for r in conn.execute(
            "SELECT at_time, word, action FROM decisions WHERE path=?", (path,)
        ).fetchall()
    ]
    rules = {
        _norm_word(r["word"]): r["action"]
        for r in conn.execute(
            "SELECT word, action FROM word_rules WHERE path=?", (path,)
        ).fetchall()
    }
    return Decisions(per_hit, rules)


def enabled_words() -> list[str]:
    rows = connect().execute(
        "SELECT word FROM wordlist WHERE enabled=1 ORDER BY word"
    ).fetchall()
    return [r["word"] for r in rows]
