"""
Fetch tag-sets from VidAngel's API using a saved auth token.

**Untested against the live API.** It was written without network access, so the auth
header format, the response shape, and the URL-to-tag-set mapping are informed guesses.
Everything here is therefore configurable at runtime and fails loudly with the actual
status code and response body, so a wrong guess is diagnosable rather than mysterious.

The token is a bearer credential for the user's VidAngel account. It is stored in the
local SQLite DB and never logged — `_redact()` strips it from anything that could be
surfaced in an error message.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_API = "https://api.vidangel.com/api/bff/tag-sets/{id}/"

#: work_id -> tag_set_id. Verified working 2026-07-26.
#:
#: The tag-set id lives under `offerings[].tag_set_id`, one offering per streaming service.
#: Different services can carry different cuts of the same title, hence different tag-sets;
#: each offering also names its service so the user can pick the one matching their copy.
#:
#: Movies and episodes use different endpoints, and the wrong one errors rather than
#: returning empty: `/episodes/?show_id=<movie id>` answers HTTP 500.
MOVIE_API = "https://api.vidangel.com/api/content/v2/movies/{id}/"
#: Whole-show listing: `seasons[].episodes[]`, every season and episode in one response.
SHOW_API = "https://api.vidangel.com/api/content/v2/shows/{id}/"
#: Single "up next" episode. `next_only=true` is REQUIRED — any other query parameter
#: (season_number, limit, page…) makes this endpoint answer HTTP 500, and omitting it
#: entirely 500s too. Kept only as a fallback; SHOW_API returns everything.
EPISODES_API = "https://api.vidangel.com/api/content/v2/episodes/?show_id={id}&next_only=true"

#: Title search. Verified working 2026-07-26. Returns three branches:
#:   "titles"               - filterable results, best matches first. Each carries
#:                            id (work_id), title, year, slug, type, tag_count.
#:   "unavailable.titles"   - titles VidAngel cannot filter, each with a `reason`.
#:                            A definitive negative: no point hunting further.
#:   "available"            - only aggregate facets (top_keywords, top_actors), NOT titles.
SEARCH_API = "https://api.vidangel.com/api/content/search/?q={query}"

#: Header templates to try, in order.
#:
#: **`Token <hex>` is the one that works** — verified against the live API on 2026-07-26.
#: VidAngel runs Django REST Framework, whose stock TokenAuthentication expects exactly
#: this. The others are kept as fallbacks in case a different credential type is pasted.
AUTH_SCHEMES = (
    ("Authorization", "Token {token}"),
    ("Authorization", "Bearer {token}"),
    ("Authorization", "JWT {token}"),
    ("Cookie", "{token}"),
)

#: A 401 whose body says credentials were *not provided* means the header form was wrong,
#: not that the token is bad — DRF reports an unrecognised scheme as anonymous. Detecting
#: that lets the next scheme be tried instead of aborting, which is what made a valid
#: `Token …` credential look expired.
_NOT_PROVIDED_MARKERS = ("were not provided", "anonymoususer", "notauthenticated")

#: Patterns that yield a tag-set or work id from a pasted URL. VidAngel watch URLs are
#: not the tag-set endpoint, so a numeric id is extracted and tried against the API.
_ID_PATTERNS = (
    re.compile(r"/tag-sets?/(\d+)"),
    re.compile(r"[?&]tag_set_id=(\d+)"),
    re.compile(r"[?&]tagSetId=(\d+)"),
    re.compile(r"/(?:watch|movies?|shows?|titles?)/(\d+)"),
    re.compile(r"[?&]work_id=(\d+)"),
    re.compile(r"(\d{4,})"),          # last resort: any long number in the URL
)


class FetchError(RuntimeError):
    """Carries enough detail to diagnose a failed fetch without leaking the token."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body[:2000]


def _redact(text: str, token: str | None) -> str:
    if token and len(token) > 6:
        return text.replace(token, f"{token[:4]}…{token[-2:]}")
    return text


@dataclass
class ParsedTarget:
    tag_set_id: int | None
    candidates: list[int]
    source_url: str


def parse_url(url: str) -> ParsedTarget:
    """Pull candidate ids out of a pasted VidAngel URL (or a bare number).

    Returns every plausible id rather than one guess: a watch URL's id may be a work id
    rather than a tag-set id, and only the API can say which resolves.
    """
    text = (url or "").strip()
    if text.isdigit():
        return ParsedTarget(int(text), [int(text)], text)

    found: list[int] = []
    for pat in _ID_PATTERNS:
        for m in pat.finditer(text):
            n = int(m.group(1))
            if n not in found:
                found.append(n)
    return ParsedTarget(found[0] if found else None, found, text)


@dataclass
class SearchHit:
    work_id: int
    title: str
    year: int | None
    kind: str                 # "movie" | "show" | ...
    slug: str = ""
    tag_count: int = 0
    filterable: bool = True
    reason: str = ""


def search(query: str, token: str | None, timeout: float = 20.0) -> list[SearchHit]:
    """Search VidAngel by title.

    Returns filterable results first, then unfilterable ones flagged `filterable=False`
    with VidAngel's stated reason — a definitive "this title cannot be filtered" is more
    useful than an empty result, since it stops the user looking further.

    Note the `available` branch contains only facets (keywords, actors), not titles; the
    real matches are in the top-level `titles` list.
    """
    from urllib.parse import quote

    url = SEARCH_API.format(query=quote(query))
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; MovieFilter/1.0)",
    })
    if token:
        req.add_header("Authorization", f"Token {token}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise FetchError(f"search failed (HTTP {exc.code})", status=exc.code,
                         body=_redact(body, token)) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise FetchError(f"search failed: {_redact(str(exc), token)}") from None

    hits: list[SearchHit] = []
    for t in data.get("titles") or []:
        if not isinstance(t, dict):
            continue
        hits.append(SearchHit(
            work_id=int(t.get("id") or 0), title=str(t.get("title") or ""),
            year=t.get("year"), kind=str(t.get("type") or ""),
            slug=str(t.get("slug") or ""), tag_count=int(t.get("tag_count") or 0),
            filterable=True,
        ))
    for t in ((data.get("unavailable") or {}).get("titles") or []):
        if not isinstance(t, dict):
            continue
        hits.append(SearchHit(
            work_id=int(t.get("id") or 0), title=str(t.get("title") or ""),
            year=t.get("year"), kind=str(t.get("type") or ""),
            filterable=False,
            reason=str(t.get("reason") or t.get("request_reason") or ""),
        ))
    return hits


def relevance(hit: SearchHit, query: str) -> int:
    """Rough match score, so an exact title beats a substring coincidence.

    VidAngel's search is loose — a query for "8 Mile" returns "18 Again" and "180" — so
    results need reordering before they are shown.
    """
    q = re.sub(r"[^a-z0-9 ]", "", query.lower()).strip()
    t = re.sub(r"[^a-z0-9 ]", "", hit.title.lower()).strip()
    if t == q:
        return 100
    if t.startswith(q) or q.startswith(t):
        return 70
    qw, tw = set(q.split()), set(t.split())
    if qw and qw <= tw:
        return 50
    if qw & tw:
        return 20 + 10 * len(qw & tw) // max(1, len(qw))
    return 0


@dataclass
class Offering:
    """One streaming service's copy of a title, with its own tag-set."""
    tag_set_id: int
    service: str
    kind: str = ""              # SUBSCRIPTION | RENTAL | PURCHASE
    max_format: str = ""


@dataclass
class WorkEntry:
    """A movie, or one episode of a show, with the tag-sets available for it."""
    work_id: int
    title: str
    kind: str
    runtime: float | None = None
    tag_count: int = 0
    season: int | None = None
    episode: int | None = None
    offerings: list[Offering] = field(default_factory=list)

    @property
    def tag_set_ids(self) -> list[int]:
        seen, out = set(), []
        for o in self.offerings:
            if o.tag_set_id and o.tag_set_id not in seen:
                seen.add(o.tag_set_id)
                out.append(o.tag_set_id)
        return out

    @property
    def label(self) -> str:
        if self.season is not None and self.episode is not None:
            return f"S{self.season:02d}E{self.episode:02d} {self.title}"
        return self.title


def _http_json(url: str, token: str | None, timeout: float = 25.0):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; MovieFilter/1.0)",
    })
    if token:
        req.add_header("Authorization", f"Token {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise FetchError(f"HTTP {exc.code} from {url}", status=exc.code,
                         body=_redact(body, token)) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise FetchError(f"request failed: {_redact(str(exc), token)}") from None


def _parse_work(raw: dict) -> WorkEntry:
    offerings = []
    for o in raw.get("offerings") or []:
        if not isinstance(o, dict):
            continue
        tsid = o.get("tag_set_id")
        if not tsid:
            continue
        offerings.append(Offering(
            tag_set_id=int(tsid),
            service=str(o.get("catalog_id") or o.get("service_id") or "?"),
            kind=str(o.get("type") or ""),
            max_format=str(o.get("max_format") or ""),
        ))
    rt = raw.get("runtime")
    return WorkEntry(
        work_id=int(raw.get("id") or 0),
        title=str(raw.get("title") or ""),
        kind=str(raw.get("type") or ""),
        runtime=float(rt) if rt else None,
        tag_count=int(raw.get("tag_count") or 0),
        season=raw.get("season_number"),
        episode=raw.get("episode_number"),
        offerings=offerings,
    )


def resolve_tagsets(
    work_id: int,
    token: str | None,
    kind: str = "",
    next_only: bool = False,
) -> list[WorkEntry]:
    """Find the tag-set ids for a work id — the link search alone cannot provide.

    Movies resolve to a single entry; shows resolve to one entry per episode. `kind`
    ("movie"/"show") comes from the search result and avoids a wasted request, but both
    endpoints are tried when it is unknown.
    """
    attempts: list[str] = []
    order = (["movie", "show"] if kind.lower().startswith("mov")
             else ["show", "movie"] if kind else ["movie", "show"])

    for which in order:
        try:
            if which == "movie":
                raw = _http_json(MOVIE_API.format(id=work_id), token)
                entry = _parse_work(raw if isinstance(raw, dict) else {})
                if entry.offerings or entry.title:
                    return [entry]
                attempts.append("movie endpoint returned no offerings")
            else:
                if next_only:
                    raw = _http_json(EPISODES_API.format(id=work_id), token)
                    rows = raw if isinstance(raw, list) else (raw.get("results") or [])
                    entries = [_parse_work(r) for r in rows if isinstance(r, dict)]
                    if entries:
                        return entries
                    attempts.append("next_only returned nothing")
                    continue

                # Whole show: seasons[].episodes[], each episode carrying its own
                # `offerings` with tag-set ids. One request covers every season, so a
                # 110-episode show resolves in a single call.
                raw = _http_json(SHOW_API.format(id=work_id), token)
                entries = []
                for season in (raw.get("seasons") or []):
                    if not isinstance(season, dict):
                        continue
                    for ep in (season.get("episodes") or []):
                        if not isinstance(ep, dict):
                            continue
                        e = _parse_work(ep)
                        e.season = season.get("number")
                        e.episode = ep.get("episode_number", len(entries) + 1)
                        entries.append(e)
                if entries:
                    return entries
                attempts.append("show endpoint listed no episodes")
        except FetchError as exc:
            # The wrong endpoint for a type answers 500, not 404, so keep going.
            attempts.append(f"{which}: {exc}")

    raise FetchError(
        f"could not resolve tag-sets for work {work_id} — {'; '.join(attempts)}"
    )


def fetch_tagset(
    target: str,
    token: str | None,
    api_template: str = DEFAULT_API,
    timeout: float = 20.0,
) -> tuple[int, str]:
    """Fetch one tag-set. Returns (tag_set_id, raw JSON text).

    Tries each candidate id from the URL and each auth scheme until a response parses as
    a tag-set. A 401/403 stops the attempt immediately — retrying other ids with a bad
    token would just multiply failed auth attempts against the account.
    """
    parsed = parse_url(target)
    if not parsed.candidates:
        raise FetchError(
            f"could not find a tag-set id in {target!r}. Paste the tag-set URL "
            f"(…/api/bff/tag-sets/<id>/) or just the numeric id."
        )

    attempts: list[str] = []
    for tag_id in parsed.candidates:
        url = (api_template.format(id=tag_id) if "{id}" in api_template
               else api_template.rstrip("/") + f"/{tag_id}/")

        for header, fmt in (AUTH_SCHEMES if token else [(None, None)]):
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                # A browser-like UA avoids the bot filtering some APIs apply to defaults.
                "User-Agent": "Mozilla/5.0 (compatible; MovieFilter/1.0)",
            })
            if header:
                req.add_header(header, fmt.format(token=token))

            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace") if exc.fp else ""
                attempts.append(f"{fmt.split()[0] if fmt else 'no auth'} -> HTTP {exc.code}")

                if exc.code in (401, 403):
                    low = body.lower()
                    wrong_scheme = any(m in low for m in _NOT_PROVIDED_MARKERS)
                    if wrong_scheme and header:
                        # The header form was not recognised; try the next scheme rather
                        # than reporting a working token as expired.
                        continue
                    raise FetchError(
                        f"VidAngel rejected the token (HTTP {exc.code}). It may have "
                        f"expired — grab a fresh one from your browser session.",
                        status=exc.code, body=_redact(body, token),
                    ) from None
                continue
            except (urllib.error.URLError, TimeoutError) as exc:
                raise FetchError(
                    f"could not reach {url}: {_redact(str(exc), token)}. If this server "
                    f"has no outbound internet access, paste the JSON instead."
                ) from None

            try:
                data = json.loads(raw)
            except ValueError:
                attempts.append(f"{header or 'no auth'} -> non-JSON response")
                continue

            # A login page or error envelope can still be valid JSON, so require the
            # fields a tag-set actually has.
            if isinstance(data, dict) and "tag_categories" in data:
                return int(data.get("tag_set_id") or tag_id), raw

            attempts.append(
                f"{header or 'no auth'} -> JSON without tag_categories "
                f"(keys: {', '.join(list(data)[:6]) if isinstance(data, dict) else type(data).__name__})"
            )

    raise FetchError(
        "no attempt returned a tag-set. Tried ids "
        f"{parsed.candidates} — {'; '.join(attempts[:8])}. "
        "If VidAngel's API has changed, paste the JSON directly instead."
    )
