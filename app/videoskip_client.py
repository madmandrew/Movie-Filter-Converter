"""
Fetch and parse filters from the VideoSkip Exchange.

**The fetch path is unverified.** Written without network access to videoskip.com, so the
endpoint, auth, and response shape are guesses. As with the VidAngel client: the endpoint
is user-configurable, failures report exactly what was attempted, and pasting a file
always works.

The *parser*, by contrast, is verified — the `.vsk` format is documented by its output in
the legacy app this project replaces, and the round-trip is covered by tests. So a user
who downloads a filter from the Exchange by hand can always use it, regardless of whether
the API guess is right.

VideoSkip format (one entry per blank-line-separated block):

    HH:MM:SS.mmm --> HH:MM:SS.mmm
    <Category> <level> (<optional description>)

Categories seen in the wild: Profane Word, Sex, Violence, Alcohol, Intense, Other.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_SEARCH = "https://videoskip.com/api/filters/search?q={query}"
DEFAULT_DOWNLOAD = "https://videoskip.com/api/filters/{id}/download"

#: VideoSkip categories mapped to whether they describe audio or video content. Audio
#: entries become mutes; video entries become cuts.
CATEGORY_KIND = {
    "profane word": "audio",
    "profanity": "audio",
    "blasphemy": "audio",
    "sex": "video",
    "nudity": "video",
    "violence": "video",
    "gore": "video",
    "intense": "video",
    "alcohol": "video",
    "drugs": "video",
    "other": "video",
}

_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-+>\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body[:2000]


@dataclass
class SkipEntry:
    start: float
    end: float
    category: str
    level: int = 1
    description: str = ""

    @property
    def kind(self) -> str:
        """"audio" (mute) or "video" (cut), from the category."""
        return CATEGORY_KIND.get(self.category.strip().lower(), "video")

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class SkipFile:
    entries: list[SkipEntry] = field(default_factory=list)
    title: str = ""
    runtime: float | None = None

    def audio(self) -> list[SkipEntry]:
        return [e for e in self.entries if e.kind == "audio"]

    def video(self) -> list[SkipEntry]:
        return [e for e in self.entries if e.kind == "video"]


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_vsk(text: str) -> SkipFile:
    """Parse a VideoSkip `.vsk` file.

    Tolerant by design: entries may be separated by blank lines or run together, the
    category line is optional, and both `.` and `,` decimal separators appear in the wild.
    """
    out = SkipFile()
    lines = [ln.rstrip() for ln in text.splitlines()]

    i = 0
    while i < len(lines):
        m = _TIME_RE.search(lines[i])
        if not m:
            # Metadata lines some exports carry at the top.
            low = lines[i].lower()
            if low.startswith("title:"):
                out.title = lines[i].split(":", 1)[1].strip()
            elif low.startswith("runtime:"):
                try:
                    out.runtime = float(lines[i].split(":", 1)[1].strip())
                except ValueError:
                    pass
            i += 1
            continue

        start = _to_seconds(*m.group(1, 2, 3, 4))
        end = _to_seconds(*m.group(5, 6, 7, 8))

        category, level, desc = "Other", 1, ""
        if i + 1 < len(lines) and lines[i + 1].strip() and not _TIME_RE.search(lines[i + 1]):
            meta = lines[i + 1].strip()
            # e.g. "Profane Word 1 (damn)"
            dm = re.match(r"(.+?)\s+(\d+)\s*(?:\((.*)\))?\s*$", meta)
            if dm:
                category = dm.group(1).strip()
                level = int(dm.group(2))
                desc = (dm.group(3) or "").strip()
            else:
                category = meta
            i += 1

        if end > start:
            out.entries.append(SkipEntry(start=start, end=end, category=category,
                                         level=level, description=desc))
        i += 1

    return out


def parse_edl(text: str) -> SkipFile:
    """Parse an MPlayer/Plex EDL: `<start> <end> <action>`, action 1 = mute, 0 = cut."""
    out = SkipFile()
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) < 3:
            continue
        try:
            s, e, action = float(parts[0]), float(parts[1]), int(float(parts[2]))
        except ValueError:
            continue
        if e <= s:
            continue
        out.entries.append(SkipEntry(
            start=s, end=e,
            category="Profane Word" if action == 1 else "Other",
        ))
    return out


def parse_any(text: str) -> SkipFile:
    """Detect and parse whichever supported format `text` is in."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty filter file")

    if stripped.startswith("{") or stripped.startswith("["):
        return _parse_json(stripped)
    if _TIME_RE.search(stripped):
        return parse_vsk(stripped)

    parsed = parse_edl(stripped)
    if parsed.entries:
        return parsed
    raise ValueError(
        "unrecognised filter format — expected VideoSkip (HH:MM:SS.mmm --> …), "
        "EDL (start end action), or JSON"
    )


def _parse_json(text: str) -> SkipFile:
    """Parse a JSON filter export. Field names vary, so several spellings are accepted."""
    data = json.loads(text)
    rows = data if isinstance(data, list) else (
        data.get("entries") or data.get("filters") or data.get("skips") or [])
    out = SkipFile(title=(data.get("title", "") if isinstance(data, dict) else ""))
    for r in rows:
        if not isinstance(r, dict):
            continue
        s = r.get("start", r.get("start_time", r.get("from")))
        e = r.get("end", r.get("end_time", r.get("to")))
        if s is None or e is None:
            continue
        try:
            s, e = float(s), float(e)
        except (TypeError, ValueError):
            continue
        if e <= s:
            continue
        out.entries.append(SkipEntry(
            start=s, end=e,
            category=str(r.get("category", r.get("type", "Other"))),
            level=int(r.get("level", 1) or 1),
            description=str(r.get("description", r.get("desc", "")) or ""),
        ))
    return out


def fetch(url: str, token: str | None = None, timeout: float = 20.0) -> str:
    """Download a filter file by URL. Returns the raw text for `parse_any`."""
    req = urllib.request.Request(url, headers={
        "Accept": "text/plain, application/json;q=0.9, */*;q=0.8",
        "User-Agent": "Mozilla/5.0 (compatible; MovieFilter/1.0)",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise FetchError(
            f"HTTP {exc.code} from {url}. If the Exchange needs a login, add a token, "
            f"or download the filter in a browser and paste it instead.",
            status=exc.code, body=body,
        ) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(
            f"could not reach {url}: {exc}. Paste the filter file instead."
        ) from None
