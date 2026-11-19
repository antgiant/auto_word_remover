#!/usr/bin/env python
"""
subdl.py - fetch a subtitle from SubDL (subdl.com) for clean.py.

A SECOND, independent subtitle source alongside opensubtitles.py - wired into
clean.py's fallback chain to run whenever OpenSubtitles didn't produce a
usable result, not only when OpenSubtitles is entirely unconfigured: no key,
no match, a daily quota exhausted (OpenSubtitlesQuotaExceeded), or its own
resync validation rejected a wrong-film match all fall through to this module
instead of leaving the file with no subtitle corroboration at all. Confirmed
2026-11-19 that Podnapisi.net (the other long-standing free alternative) has
been unreachable since ~2026-03-26 - domain stopped resolving - so it wasn't
a viable second option; SubDL is the one still-live, similarly free, key-
based service with a documented-enough JSON API. Subscene/Addic7ed have no
stable public API at all (scraping-only, aggressive anti-bot), and a general
multi-provider library (subliminal) was deliberately NOT adopted - it would
pull in a real dependency tree (babelfish, guessit, requests, dogpile.cache,
rarfile, click, ...) against this project's established "every module here
is standalone, pure stdlib" posture (see opensubtitles.py's own docstring).

Free API key: sign up at https://subdl.com (no payment, instant key from the
API section of account settings) and set SUBDL_API_KEY, or drop a plain-text
file "subdl.key" next to this script (one line, nothing else - gitignored,
never committed) - same two-step pattern as opensubtitles.py's api_key().
Free-tier limits aren't published precisely; word-of-mouth as of 2026-11 is
roughly 2000 searches / 50 downloads a day, independent of OpenSubtitles'
own quota - so a file that's merely unlucky on one service still has a real
shot at the other.

Same trust posture as opensubtitles.py: nothing returned here is used for
its own timestamps - clean.py always resyncs it to this recording's own
transcript via flag_language.resync_units_to_transcript() before use (see
clean.py's resync_external_srt, which independently guards against a
wrong-film match via _MIN_RESYNC_MATCH_FRACTION, on top of this module's own
title-match guard in fetch_subtitle() below - SubDL's `subtitles` array is
already scoped server-side to just the FIRST matched title in `results`, so
unlike opensubtitles.search() there's no per-subtitle title to re-rank
against; the only guard available is checking `results[0]` itself before
trusting anything in `subtitles` at all).
"""
from __future__ import annotations

import difflib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from opensubtitles import guess_query, LANG_2TO3, LANG_3TO2   # shared, provider-agnostic filename
#   parsing + language-code tables - reused rather than duplicated since both modules already
#   live in this same project (not a new third-party dependency)

API_BASE = "https://api.subdl.com/api/v1/subtitles"
DL_BASE = "https://dl.subdl.com"
USER_AGENT = "auto_word_remover/1.0"


class SubDLError(RuntimeError):
    pass


class SubDLQuotaExceeded(SubDLError):
    """Best-effort heuristic (HTTP 406/429, or "quota"/"limit" anywhere in the response body) -
    not yet validated against a real quota-exceeded response from the live API, mirroring
    opensubtitles.OpenSubtitlesQuotaExceeded's own same caveat. Tighten this if a real response
    is ever seen that doesn't match."""
    pass


def api_key() -> str | None:
    key = os.environ.get("SUBDL_API_KEY", "").strip()
    if key:
        return key
    key_file = HERE / "subdl.key"
    if key_file.is_file():
        text = key_file.read_text(encoding="utf-8-sig").strip()
        if text:
            return text
    return None


def _request(params: dict, key: str) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in {**params, "api_key": key}.items()
                                 if v not in (None, "")})
    url = f"{API_BASE}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        msg = f"GET /subtitles -> HTTP {exc.code}: {detail}"
        if exc.code in (406, 429) or "quota" in detail.lower() or "limit" in detail.lower():
            raise SubDLQuotaExceeded(msg) from exc
        raise SubDLError(msg) from exc
    except urllib.error.URLError as exc:
        raise SubDLError(f"GET /subtitles -> {exc.reason}") from exc
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SubDLError("GET /subtitles -> bad JSON response") from exc
    if data.get("status") is False:
        raise SubDLError(f"GET /subtitles -> {data.get('message') or data!r}")
    return data


def search(query: dict, lang2: str, key: str) -> dict:
    """Returns the raw {"results": [...], "subtitles": [...]} response.
    `unpack=1` asks SubDL to also expose direct per-file links (`unpack_files`)
    alongside the normal zipped `subtitles[].url` - download() below checks
    the actual downloaded bytes for a zip signature rather than trusting
    either shape blindly, since SubDL's own docs don't precisely specify when
    each is populated."""
    params = {
        "film_name": query.get("query"),
        "type": "tv" if query.get("season_number") is not None else "movie",
        "languages": lang2.upper(),
        "unpack": 1,
    }
    for k in ("season_number", "episode_number", "year"):
        if query.get(k) is not None:
            params[k] = query[k]
    return _request(params, key)


def _normalize_title(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


_MIN_TITLE_MATCH_RATIO = 0.6   # same bar and same reasoning as opensubtitles.best_title_match -
#   see this module's docstring for why SubDL needs this check on `results[0]` specifically
#   (its `subtitles` array has no title of its own to re-rank by).


def _unzip_first_srt(raw: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".srt"):
                    return zf.read(name).decode("utf-8-sig", errors="replace")
    except zipfile.BadZipFile:
        return None
    return None


def download(path_or_url: str) -> str:
    """`path_or_url` is a subtitle entry's own "url" field - either a path
    relative to DL_BASE (the normal zipped download) or, occasionally, an
    already-absolute direct file URL (seen with unpack=1). Whether the
    result is a zip is checked by content (a leading "PK" signature), not by
    guessing from the URL shape, since that isn't documented precisely
    enough to trust."""
    url = path_or_url if path_or_url.startswith("http") else f"{DL_BASE}{path_or_url}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.URLError as exc:
        raise SubDLError(f"downloading subtitle file failed ({exc})") from exc
    if raw[:2] == b"PK":
        text = _unzip_first_srt(raw)
        if text is None:
            raise SubDLError("downloaded a zip with no .srt inside")
        return text
    return raw.decode("utf-8-sig", errors="replace")


def fetch_subtitle(media: Path, cfg, out_dir: Path | None = None,
                   force: bool = False) -> tuple[Path, dict] | None:
    """Same contract as opensubtitles.fetch_subtitle(): search + download the
    best-matching subtitle for `media`, cached next to it as
    "<name>.<lang>.subdl.raw.srt" (+ a small .meta.json) so reruns don't
    re-hit the network/quota. Returns (raw_srt_path, meta) or None - never
    raises; a missing key, failed search, quota, or network error just means
    "no SubDL source this run", same posture as every other optional
    subtitle fallback in this pipeline.

    This is the RAW download, unmodified - its timestamps are not to be
    trusted (see module docstring). clean.py runs it through
    flag_language.resync_units_to_transcript() before using it for
    anything."""
    key = api_key()
    if not key:
        print("  [warn] no SubDL API key (set SUBDL_API_KEY, or drop one in "
              f"{HERE / 'subdl.key'}) - get a free one at https://subdl.com - skipping SubDL "
              f"lookup", file=sys.stderr)
        return None

    lang2 = (getattr(cfg, "subdl_lang", "") or getattr(cfg, "opensubtitles_lang", "") or "en").lower()
    base = out_dir or media.parent
    raw_path = base / f"{media.stem}.{lang2}.subdl.raw.srt"
    meta_path = base / f"{media.stem}.{lang2}.subdl.meta.json"
    if raw_path.is_file() and meta_path.is_file() and not force:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            print(f"  SubDL: reusing cached {raw_path.name}")
            return raw_path, meta
        except (OSError, json.JSONDecodeError):
            pass

    try:
        q = dict(guess_query(media))
        override = (getattr(cfg, "subdl_query", "") or "").strip()
        if override:
            q["query"] = override
        if not q.get("query"):
            print("  [warn] could not guess a search title from the filename for SubDL - "
                  "set --subdl-query", file=sys.stderr)
            return None
        data = search(q, lang2, key)
        results = data.get("results") or []
        if not results:
            print(f"  [warn] SubDL: no results for {q!r}", file=sys.stderr)
            return None
        top = results[0]
        candidate_title = top.get("name") or top.get("original_name") or ""
        if difflib.SequenceMatcher(
                None, _normalize_title(q["query"]), _normalize_title(candidate_title)
        ).ratio() < _MIN_TITLE_MATCH_RATIO:
            print(f"  [warn] SubDL: top match {candidate_title!r} doesn't closely enough match "
                  f"{q['query']!r} - discarding rather than risk a wrong film", file=sys.stderr)
            return None
        subs = data.get("subtitles") or []
        if not subs:
            print(f"  [warn] SubDL: {candidate_title!r} matched but has no subtitle(s) in "
                  f"[{lang2}]", file=sys.stderr)
            return None
        best = subs[0]
        text = download(best["url"])
        meta = {"sd_id": top.get("sd_id"), "query": q,
                "release": best.get("release_name") or best.get("name"),
                "matched_title": candidate_title, "matched_by": "search"}
        raw_path.write_text(text, encoding="utf-8")
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"  SubDL: fetched {meta['release']!r} (matched {candidate_title!r}) -> "
              f"{raw_path.name}")
        return raw_path, meta
    except SubDLQuotaExceeded as exc:
        print(f"  [warn] SubDL: quota exceeded ({exc}) - continuing without it", file=sys.stderr)
        return None
    except SubDLError as exc:
        print(f"  [warn] SubDL fetch failed ({exc}) - continuing without it", file=sys.stderr)
        return None
