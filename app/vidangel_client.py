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
from dataclasses import dataclass

DEFAULT_API = "https://api.vidangel.com/api/bff/tag-sets/{id}/"

#: Header templates to try, in order. VidAngel's own client sends a bearer token, but
#: token-style APIs vary and the user may paste a cookie value instead, so try the
#: plausible forms rather than failing on the first.
AUTH_SCHEMES = (
    ("Authorization", "Bearer {token}"),
    ("Authorization", "Token {token}"),
    ("Authorization", "JWT {token}"),
    ("Cookie", "{token}"),
)

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
                attempts.append(f"{header or 'no auth'} -> HTTP {exc.code}")
                if exc.code in (401, 403):
                    # Bad or expired credentials: stop rather than hammering the account.
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
