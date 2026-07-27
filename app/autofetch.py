"""
Automatically find and cache VidAngel filters for library titles.

Chains the verified pieces — parse filename, search, resolve work to tag-sets, fetch — and
adds the judgement about *when it is safe to do so without asking*.

Conservative on purpose: fetching the wrong filter set is worse than fetching none,
because a plausible-looking tag-set for the wrong film would produce mutes at meaningless
times. A title must match closely and, when both years are known, agree on year. Anything
short of that is recorded as a suggestion for the user to confirm.

Results are cached per title so a re-scan does not re-query VidAngel for thousands of
files, and so a "no filters exist" answer is remembered rather than rediscovered.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import db
import titleparse
import vidangel_client as vac


@dataclass
class MatchResult:
    path: str
    parsed: str
    status: str          # fetched | suggested | none | unfilterable | error
    detail: str = ""
    work_id: int | None = None
    tag_set_id: int | None = None
    score: int = 0
    candidates: list[dict] | None = None


def _token() -> str | None:
    return db.get_setting("vidangel_token")


def _cache_get(path: str) -> dict | None:
    row = db.connect().execute(
        "SELECT status, work_id, tag_set_id, score, detail, checked_at "
        "FROM autofetch WHERE path=?", (path,)
    ).fetchone()
    return dict(row) if row else None


def _cache_put(path: str, res: MatchResult) -> None:
    with db.tx() as c:
        c.execute(
            """INSERT INTO autofetch(path, status, work_id, tag_set_id, score, detail,
                                     checked_at)
               VALUES (?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(path) DO UPDATE SET
                   status=excluded.status, work_id=excluded.work_id,
                   tag_set_id=excluded.tag_set_id, score=excluded.score,
                   detail=excluded.detail, checked_at=excluded.checked_at""",
            (path, res.status, res.work_id, res.tag_set_id, res.score, res.detail),
        )


def match_one(path: str, name: str, duration: float | None = None,
              auto: bool = True, force: bool = False) -> MatchResult:
    """Find (and optionally fetch) the VidAngel filters for one title.

    `duration` picks between multiple offerings by comparing runtimes — useful, though the
    offset estimator makes an imperfect choice recoverable.
    """
    if not force:
        cached = _cache_get(path)
        if cached and cached["status"] in ("fetched", "unfilterable", "none"):
            return MatchResult(path=path, parsed=name, status=cached["status"],
                               detail=cached["detail"] or "cached",
                               work_id=cached["work_id"],
                               tag_set_id=cached["tag_set_id"],
                               score=cached["score"] or 0)

    token = _token()
    if not token:
        return MatchResult(path, name, "error", "no VidAngel token saved")

    p = titleparse.parse(name)
    if not p.title:
        return MatchResult(path, name, "error", "could not parse a title")

    try:
        hits = vac.search(p.title, token)
    except vac.FetchError as exc:
        return MatchResult(path, str(p), "error", f"search failed: {exc}")

    scored = []
    for h in hits:
        s = titleparse.score_match(p, h.title, h.year)
        if s > 0:
            scored.append((s, h))
    scored.sort(key=lambda x: (-x[0], not x[1].filterable))

    if not scored:
        res = MatchResult(path, str(p), "none", "no VidAngel title matched")
        _cache_put(path, res)
        return res

    top_score, top = scored[0]

    if not top.filterable and top_score >= titleparse.AUTO_THRESHOLD:
        res = MatchResult(path, str(p), "unfilterable",
                          top.reason or "VidAngel has no filters for this title",
                          work_id=top.work_id, score=top_score)
        _cache_put(path, res)
        return res

    candidates = [
        {"work_id": h.work_id, "title": h.title, "year": h.year,
         "score": s, "filterable": h.filterable, "tag_count": h.tag_count}
        for s, h in scored[:5]
    ]

    if top_score < titleparse.AUTO_THRESHOLD or not auto:
        res = MatchResult(path, str(p), "suggested",
                          f"best match {top.title!r} ({top.year}) scored {top_score}, "
                          f"below the {titleparse.AUTO_THRESHOLD} auto threshold",
                          work_id=top.work_id, score=top_score, candidates=candidates)
        _cache_put(path, res)
        return res

    # Confident: resolve the work to its tag-sets and fetch.
    try:
        entries = vac.resolve_tagsets(top.work_id, token, kind=top.kind)
    except vac.FetchError as exc:
        res = MatchResult(path, str(p), "error", f"resolve failed: {exc}",
                          work_id=top.work_id, score=top_score)
        _cache_put(path, res)
        return res

    entry = _pick_entry(entries, p)
    if entry is None:
        res = MatchResult(path, str(p), "none",
                          f"matched {top.title!r} but found no entry for "
                          f"{'S%02dE%02d' % (p.season, p.episode) if p.is_episode else 'it'}",
                          work_id=top.work_id, score=top_score)
        _cache_put(path, res)
        return res

    tag_ids = entry.tag_set_ids
    if not tag_ids:
        res = MatchResult(path, str(p), "none", "no tag-sets published for this entry",
                          work_id=top.work_id, score=top_score)
        _cache_put(path, res)
        return res

    chosen = _pick_tagset(tag_ids, duration, token)
    try:
        tsid, raw = vac.fetch_tagset(str(chosen), token)
    except vac.FetchError as exc:
        res = MatchResult(path, str(p), "error", f"fetch failed: {exc}",
                          work_id=top.work_id, score=top_score)
        _cache_put(path, res)
        return res

    import vidangel as va

    parsed_ts = va.parse(raw)
    with db.tx() as c:
        c.execute(
            """INSERT INTO tagsets(tag_set_id, work_id, title_hint, runtime, payload,
                                   added_at)
               VALUES (?,?,?,?,?,datetime('now'))
               ON CONFLICT(tag_set_id) DO UPDATE SET
                   payload=excluded.payload, title_hint=excluded.title_hint,
                   runtime=excluded.runtime""",
            (parsed_ts.tag_set_id, parsed_ts.work_id, entry.label,
             parsed_ts.runtime_unaltered, raw),
        )
        c.execute("UPDATE titles SET tag_set_id=? WHERE path=?",
                  (parsed_ts.tag_set_id, path))

    detail = f"{entry.label} — {len(parsed_ts.incidents)} incidents"
    if duration and parsed_ts.runtime_unaltered:
        detail += f", runtime delta {duration - parsed_ts.runtime_unaltered:+.0f}s"
    res = MatchResult(path, str(p), "fetched", detail, work_id=top.work_id,
                      tag_set_id=parsed_ts.tag_set_id, score=top_score)
    _cache_put(path, res)
    return res


def fetch_for_work(path: str, name: str, work_id: int, kind: str = "",
                   duration: float | None = None,
                   tag_set_id: int | None = None) -> MatchResult:
    """Fetch filters for a work the user picked, bypassing the score threshold.

    Used when automatic matching was not confident enough — the user has looked at the
    candidates and decided, so their choice is authoritative. `tag_set_id` may be given
    directly to skip resolution when the user picked a specific offering.
    """
    token = _token()
    if not token:
        return MatchResult(path, name, "error", "no VidAngel token saved")

    p = titleparse.parse(name)
    entry_label = name

    if tag_set_id is None:
        try:
            entries = vac.resolve_tagsets(work_id, token, kind=kind)
        except vac.FetchError as exc:
            return MatchResult(path, str(p), "error", f"resolve failed: {exc}",
                               work_id=work_id)
        entry = _pick_entry(entries, p)
        if entry is None:
            want = (f"S{p.season:02d}E{p.episode:02d}" if p.is_episode else "this title")
            return MatchResult(path, str(p), "none",
                               f"no entry for {want} under that work", work_id=work_id)
        if not entry.tag_set_ids:
            return MatchResult(path, str(p), "none",
                               "no tag-sets published for that entry", work_id=work_id)
        tag_set_id = _pick_tagset(entry.tag_set_ids, duration, token)
        entry_label = entry.label

    try:
        _tsid, raw = vac.fetch_tagset(str(tag_set_id), token)
    except vac.FetchError as exc:
        return MatchResult(path, str(p), "error", f"fetch failed: {exc}",
                           work_id=work_id)

    import vidangel as va

    parsed_ts = va.parse(raw)
    with db.tx() as c:
        c.execute(
            """INSERT INTO tagsets(tag_set_id, work_id, title_hint, runtime, payload,
                                   added_at)
               VALUES (?,?,?,?,?,datetime('now'))
               ON CONFLICT(tag_set_id) DO UPDATE SET
                   payload=excluded.payload, title_hint=excluded.title_hint,
                   runtime=excluded.runtime""",
            (parsed_ts.tag_set_id, parsed_ts.work_id, entry_label,
             parsed_ts.runtime_unaltered, raw),
        )
        c.execute("UPDATE titles SET tag_set_id=? WHERE path=?",
                  (parsed_ts.tag_set_id, path))

    detail = f"{entry_label} — {len(parsed_ts.incidents)} incidents (picked manually)"
    if duration and parsed_ts.runtime_unaltered:
        detail += f", runtime delta {duration - parsed_ts.runtime_unaltered:+.0f}s"
    res = MatchResult(path, str(p), "fetched", detail, work_id=work_id,
                      tag_set_id=parsed_ts.tag_set_id, score=100)
    _cache_put(path, res)
    return res


def candidates_for(path: str, name: str, query: str | None = None) -> dict:
    """Search results for a title, scored against the filename, for manual selection.

    `query` overrides the parsed title — release naming does not always resemble the
    catalogue's, so the user needs to be able to retype it.
    """
    token = _token()
    if not token:
        raise ValueError("no VidAngel token saved")

    p = titleparse.parse(name)
    q = (query or p.title).strip()
    if not q:
        raise ValueError("nothing to search for")

    hits = vac.search(q, token)
    scored = sorted(
        ((titleparse.score_match(p, h.title, h.year), h) for h in hits),
        key=lambda x: (-x[0], not x[1].filterable),
    )
    return {
        "query": q,
        "parsed": str(p),
        "parsed_title": p.title,
        "season": p.season,
        "episode": p.episode,
        "results": [
            {"work_id": h.work_id, "title": h.title, "year": h.year, "kind": h.kind,
             "tag_count": h.tag_count, "filterable": h.filterable,
             "reason": h.reason, "score": s,
             "auto": s >= titleparse.AUTO_THRESHOLD and h.filterable}
            for s, h in scored[:25]
        ],
    }


def _pick_entry(entries: list, p: titleparse.ParsedTitle):
    """Choose the episode matching the filename, or the sole movie entry."""
    if not entries:
        return None
    if not p.is_episode:
        return entries[0]
    for e in entries:
        if e.season == p.season and e.episode == p.episode:
            return e
    return None


def _pick_tagset(tag_ids: list[int], duration: float | None, token: str | None) -> int:
    """Pick the tag-set whose runtime is closest to the local file.

    Only worth a network round-trip when there is a real choice AND a duration to compare
    against. An imperfect pick is recoverable anyway — `tools/offset.py` measures the true
    offset from the audio — so this is an optimisation, not a correctness requirement.
    """
    if len(tag_ids) == 1 or not duration:
        return tag_ids[0]

    best, best_delta = tag_ids[0], None
    for tid in tag_ids:
        try:
            _id, raw = vac.fetch_tagset(str(tid), token)
        except vac.FetchError:
            continue
        import json

        rt = float(json.loads(raw).get("runtime_unaltered") or 0)
        if not rt:
            continue
        delta = abs(duration - rt)
        if best_delta is None or delta < best_delta:
            best, best_delta = tid, delta
    return best


def sweep(paths: list[tuple[str, str, float | None]], auto: bool = True,
          progress=None, delay: float = 0.4) -> list[MatchResult]:
    """Match many titles, politely.

    `delay` throttles requests: a library sweep can be thousands of titles and there is no
    reason to hammer someone else's API. Cached answers skip the network entirely.
    """
    out = []
    for i, (path, name, dur) in enumerate(paths):
        cached = _cache_get(path)
        res = match_one(path, name, dur, auto=auto)
        out.append(res)
        if progress:
            progress(i + 1, len(paths), res)
        # Only sleep when we actually went to the network.
        if not cached and res.status != "error":
            time.sleep(delay)
    return out
