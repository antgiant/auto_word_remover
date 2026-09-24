#!/usr/bin/env python
"""
opensubtitles.py - fetch a subtitle from OpenSubtitles for clean.py.

Standalone, pure stdlib (urllib), same as every other module in this project.
Used as the last-resort subtitle source in clean.py's fallback chain (text
track -> CC608 -> PGS OCR -> VobSub OCR -> OpenSubtitles), for sources with no
usable local subtitle at all - the common case for TV recordings.

Two-step API key: an environment variable OPENSUBTITLES_API_KEY, or (simpler
for a personal one-machine setup) a plain-text file "opensubtitles.key" next
to this script, one line, nothing else - gitignored, so it never gets
committed. Get a free key at https://www.opensubtitles.com/en/consumers
("API Consumers" in account settings).

Optional OPENSUBTITLES_USERNAME / OPENSUBTITLES_PASSWORD env vars log in for
a higher daily download quota; without them, downloads use the (much lower)
anonymous quota tied to the API key alone.

IMPORTANT: what this module fetches is NOT trusted for timing at all - a
subtitle authored for a theatrical/streaming release does not share a clock
with a TV recording of the same content (commercial breaks, station edits,
plain drift), and the file itself often carries injected ad/attribution
cues. See flag_language.resync_units_to_transcript() for how the real
timing is rebuilt from a whole-file text alignment against the ASR
transcript instead of anything in this file's own timestamps.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "auto_word_remover/1.0"

# ISO 639-1 (OpenSubtitles' query/response language) <-> ISO 639-2 (mkvmerge/
# this project's LANG_NAMES) - just the languages LANG_NAMES already knows.
LANG_2TO3 = {
    "en": "eng", "es": "spa", "fr": "fre", "de": "ger", "it": "ita",
    "ja": "jpn", "pt": "por", "ru": "rus", "zh": "chi", "ko": "kor", "nl": "dut",
}
LANG_3TO2 = {v: k for k, v in LANG_2TO3.items()}


class OpenSubtitlesError(RuntimeError):
    pass


class OpenSubtitlesQuotaExceeded(OpenSubtitlesError):
    """The API rejected a call because the daily download quota is used up
    (as opposed to a network error, bad key, or a genuine no-match search) -
    a caller can use this to stop attempting further downloads for the rest
    of a batch run rather than burning time/requests on calls doomed to fail
    the same way, and queue the rest for a retry once the quota resets.

    Detection is a best-effort heuristic (HTTP 406/429, or "quota" anywhere
    in the response body) - not yet validated against a real quota-exceeded
    response from the live API, since deliberately exhausting the quota to
    check the exact wording wasn't done. Tighten this if a real response is
    ever seen that doesn't match."""
    pass


def api_key() -> str | None:
    key = os.environ.get("OPENSUBTITLES_API_KEY", "").strip()
    if key:
        return key
    key_file = HERE / "opensubtitles.key"
    if key_file.is_file():
        text = key_file.read_text(encoding="utf-8-sig").strip()
        if text:
            return text
    return None


def _request(method: str, path: str, key: str, token: str | None = None,
             params: dict | None = None, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if qs:
            url += "?" + qs
    headers = {"Api-Key": key, "User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        msg = f"{method} {path} -> HTTP {exc.code}: {detail}"
        if exc.code in (406, 429) or "quota" in detail.lower():
            raise OpenSubtitlesQuotaExceeded(msg) from exc
        raise OpenSubtitlesError(msg) from exc
    except urllib.error.URLError as exc:
        raise OpenSubtitlesError(f"{method} {path} -> {exc.reason}") from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise OpenSubtitlesError(f"{method} {path} -> bad JSON response") from exc


def login(key: str) -> str | None:
    """Bearer token from OPENSUBTITLES_USERNAME/PASSWORD, or None (anonymous -
    downloads still work, just against a much lower daily quota). Never
    raises - a login failure just means "stay anonymous this run"."""
    user = os.environ.get("OPENSUBTITLES_USERNAME", "").strip()
    pw = os.environ.get("OPENSUBTITLES_PASSWORD", "").strip()
    if not user or not pw:
        return None
    try:
        data = _request("POST", "/login", key, body={"username": user, "password": pw})
        return data.get("token")
    except OpenSubtitlesError as exc:
        print(f"  [warn] OpenSubtitles login failed, continuing anonymously ({exc})", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# query guessing
# ---------------------------------------------------------------------------
_SEASON_EP = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_YEAR = re.compile(r"\((19|20)\d{2}\)")
_NOISE = re.compile(
    r"\[[^\]]*(4K|1080p|720p|2160p|HDR|HDTV|WEB[- ]?DL|WEBRip|BluRay|x264|x265|HEVC)[^\]]*\]|"
    r"\((Cleaned|Wordless|Original|Extended|Unrated|Director'?s?\s*Cut|DVD[_ ]?t\d+)\)",
    re.IGNORECASE)


def guess_query(media: Path) -> dict:
    """Best-effort title/season/episode/year parse from a filename - just a
    starting point for search(); always overridable via
    --opensubtitles-query / cfg.opensubtitles_query, or bypass search
    entirely with --opensubtitles-id."""
    name = _NOISE.sub(" ", media.stem)
    season = episode = year = None
    m = _SEASON_EP.search(name)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        title = name[: m.start()]
    else:
        ym = _YEAR.search(name)
        title = name[: ym.start()] if ym else name
        if ym:
            year = int(ym.group(0).strip("()"))
    title = re.sub(r"[._]+", " ", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" -")
    return {"query": title, "season_number": season, "episode_number": episode, "year": year}


# ---------------------------------------------------------------------------
# search / download
# ---------------------------------------------------------------------------
def search(query: dict, lang2: str, key: str) -> list[dict]:
    params = {"query": query.get("query"), "languages": lang2}
    for k in ("season_number", "episode_number", "year"):
        if query.get(k) is not None:
            params[k] = query[k]
    data = _request("GET", "/subtitles", key, params=params)
    results = data.get("data") or []
    results.sort(key=lambda r: (r.get("attributes", {}).get("download_count") or 0), reverse=True)
    return results


def download(file_id: int, key: str, token: str | None) -> str:
    data = _request("POST", "/download", key, token=token, body={"file_id": int(file_id)})
    link = data.get("link")
    if not link:
        raise OpenSubtitlesError(f"no download link in response: {data}")
    req = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if resp.info().get("Content-Encoding") == "gzip" or link.lower().endswith(".gz"):
                raw = gzip.decompress(raw)
    except urllib.error.URLError as exc:
        raise OpenSubtitlesError(f"downloading subtitle file failed ({exc})") from exc
    return raw.decode("utf-8-sig", errors="replace")


def fetch_subtitle(media: Path, cfg, out_dir: Path | None = None,
                   force: bool = False) -> tuple[Path, dict] | None:
    """Search + download the best-matching subtitle for `media`, cached next
    to it as "<name>.<lang>.opensubtitles.raw.srt" (+ a small .meta.json of
    what matched) so reruns don't re-hit the network/quota. Returns
    (raw_srt_path, meta) or None - never raises; a missing key, failed
    search, quota, or network error just means "no OpenSubtitles source this
    run", same as every other optional subtitle fallback in this pipeline.

    This is the RAW download, unmodified - its timestamps are not to be
    trusted (see module docstring). clean.py runs it through
    flag_language.resync_units_to_transcript() before using it for
    anything."""
    key = api_key()
    if not key:
        print("  [warn] no OpenSubtitles API key (set OPENSUBTITLES_API_KEY, or drop one in "
              f"{HERE / 'opensubtitles.key'}) - skipping OpenSubtitles lookup", file=sys.stderr)
        return None

    lang2 = (getattr(cfg, "opensubtitles_lang", "") or "en").lower()
    base = out_dir or media.parent
    raw_path = base / f"{media.stem}.{lang2}.opensubtitles.raw.srt"
    meta_path = base / f"{media.stem}.{lang2}.opensubtitles.meta.json"
    if raw_path.is_file() and meta_path.is_file() and not force:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            print(f"  OpenSubtitles: reusing cached {raw_path.name}")
            return raw_path, meta
        except (OSError, json.JSONDecodeError):
            pass

    explicit_id = (getattr(cfg, "opensubtitles_id", "") or "").strip()
    try:
        token = login(key)
        if explicit_id:
            text = download(explicit_id, key, token)
            meta = {"file_id": explicit_id, "query": None, "release": None,
                    "download_count": None, "matched_by": "explicit-id"}
        else:
            q = dict(guess_query(media))
            override = (getattr(cfg, "opensubtitles_query", "") or "").strip()
            if override:
                q["query"] = override
            if not q.get("query"):
                print("  [warn] could not guess a search title from the filename for "
                      "OpenSubtitles - set --opensubtitles-query", file=sys.stderr)
                return None
            results = search(q, lang2, key)
            if not results:
                print(f"  [warn] OpenSubtitles: no results for {q!r}", file=sys.stderr)
                return None
            attrs = results[0].get("attributes", {})
            files = attrs.get("files") or []
            if not files:
                print("  [warn] OpenSubtitles: top match has no downloadable file", file=sys.stderr)
                return None
            file_id = files[0]["file_id"]
            text = download(file_id, key, token)
            meta = {"file_id": file_id, "query": q, "release": attrs.get("release"),
                    "download_count": attrs.get("download_count"), "matched_by": "search"}
        raw_path.write_text(text, encoding="utf-8")
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"  OpenSubtitles: fetched {meta.get('release') or meta.get('file_id')!r} "
              f"({meta.get('download_count', '?')} downloads) -> {raw_path.name}")
        return raw_path, meta
    except OpenSubtitlesQuotaExceeded as exc:
        print(f"  [warn] OpenSubtitles: OPENSUBTITLES_QUOTA_EXCEEDED - daily download quota "
              f"used up ({exc}) - continuing without it", file=sys.stderr)
        return None
    except OpenSubtitlesError as exc:
        print(f"  [warn] OpenSubtitles fetch failed ({exc}) - continuing without it", file=sys.stderr)
        return None
