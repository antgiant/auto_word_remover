#!/usr/bin/env python
"""
tmdb.py - look up a movie's genre/keyword tags from TMDB for clean.py.

Standalone, pure stdlib (urllib), same pattern as opensubtitles.py. Used by
clean.py to detect an overtly Christian / faith-based film so the
"irreverence" wordlist category can be swapped for the stricter
"irreverence_strict" one (see wordlists/irreverence_strict.txt): the default
irreverence.txt patterns ("sweet Jesus", "my Lord", "oh my God", "God
willing", "lord have mercy", ...) are tuned to catch casual exclamations,
but in a faith-based film those exact phrases are overwhelmingly sincere
prayer/worship, not profanity - muting them is the opposite of what this
tool is for. See clean.py's resolve_categories() for the wiring and
AGENTS.md ("Faith-based detection") for the full rationale.

Two-step API key, same convention as opensubtitles.py: an environment
variable TMDB_API_KEY, or (simpler for a personal one-machine setup) a
plain-text file "tmdb.key" next to this script, one line, nothing else -
gitignored, never committed. Get a free (v3) API key at
https://www.themoviedb.org/settings/api after creating an account.

Works for TV episodes too, not just movies: check_faith_based() reuses
opensubtitles.guess_query()'s season/episode parse to pick TMDB's /search/tv
+ /tv/{id} endpoints instead of the movie ones whenever the filename looks
like "SxxExx" - same signal opensubtitles.search() already keys its own
season_number/episode_number params off of.

Degrades gracefully everywhere: a missing key, failed search, or network
error just means "can't tell, treat as not faith-based" (None) - same
posture as every other optional integration in this project. Never raises
out of check_faith_based().
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_BASE = "https://api.themoviedb.org/3"
USER_AGENT = "auto_word_remover/1.0"

# Keyword/genre name substrings (TMDB's community-tagged /movie/{id}/keywords
# and /movie/{id} genres, matched case-insensitively as substrings) that mark
# a film as overtly Christian / faith-based. Deliberately narrow - broad
# terms like "prayer" or "pastor" would false-positive plenty of ordinary
# dramas that merely feature a religious scene or character without the film
# itself being faith content.
FAITH_KEYWORDS = (
    "christian film", "faith-based film", "faith based film", "faith-based",
    "faith based", "christian", "christianity", "gospel", "evangelical",
    "evangelism", "biblical", "bible story", "megachurch", "missionary",
)


class TmdbError(RuntimeError):
    pass


def api_key() -> str | None:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if key:
        return key
    key_file = HERE / "tmdb.key"
    if key_file.is_file():
        text = key_file.read_text(encoding="utf-8-sig").strip()
        if text:
            return text
    return None


def _request(path: str, key: str, params: dict | None = None) -> dict:
    qs_params = dict(params or {})
    qs_params["api_key"] = key
    qs = urllib.parse.urlencode({k: v for k, v in qs_params.items() if v not in (None, "")})
    url = f"{API_BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise TmdbError(f"GET {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TmdbError(f"GET {path} -> {exc.reason}") from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise TmdbError(f"GET {path} -> bad JSON response") from exc


def search_movie(query: str, year: int | None, key: str) -> dict | None:
    params = {"query": query}
    if year:
        params["year"] = year
    data = _request("/search/movie", key, params=params)
    results = data.get("results") or []
    return results[0] if results else None  # TMDB already ranks by relevance/popularity


def movie_keywords(movie_id: int, key: str) -> list[str]:
    data = _request(f"/movie/{movie_id}/keywords", key)
    return [kw.get("name", "") for kw in (data.get("keywords") or [])]


def movie_genres(movie_id: int, key: str) -> list[str]:
    data = _request(f"/movie/{movie_id}", key)
    return [g.get("name", "") for g in (data.get("genres") or [])]


def search_tv(query: str, year: int | None, key: str) -> dict | None:
    params = {"query": query}
    if year:
        params["first_air_date_year"] = year
    data = _request("/search/tv", key, params=params)
    results = data.get("results") or []
    return results[0] if results else None


def tv_keywords(series_id: int, key: str) -> list[str]:
    data = _request(f"/tv/{series_id}/keywords", key)
    # TMDB's TV keywords endpoint uses "results", NOT "keywords" like the movie
    # one does - a real, documented inconsistency in their own API, not a typo.
    return [kw.get("name", "") for kw in (data.get("results") or [])]


def tv_genres(series_id: int, key: str) -> list[str]:
    data = _request(f"/tv/{series_id}", key)
    return [g.get("name", "") for g in (data.get("genres") or [])]


def _is_faith_tag(tag: str) -> bool:
    t = tag.lower()
    return any(fk in t for fk in FAITH_KEYWORDS)


def check_faith_based(media: Path, cfg, out_dir: Path | None = None,
                      force: bool = False) -> bool | None:
    """Best-effort "is this an overtly Christian / faith-based film?" check.

    Cached next to the media as "<name>.tmdb.meta.json" (mirrors
    opensubtitles.py's own meta-caching) so a rerun doesn't re-hit the
    network/quota. Returns True/False once a TMDB match is found and
    checked, or None if it genuinely can't tell (no API key, no search
    match, network/API error) - a caller should treat None the same as
    False (fall back to the normal wordlist) and never let this block the
    pipeline."""
    base = out_dir or media.parent
    meta_path = base / f"{media.stem}.tmdb.meta.json"
    if meta_path.is_file() and not force:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta.get("faith_based")
        except (OSError, json.JSONDecodeError):
            pass

    key = api_key()
    if not key:
        print("  [warn] no TMDB API key (set TMDB_API_KEY, or drop one in "
              f"{HERE / 'tmdb.key'}) - skipping faith-based detection", file=sys.stderr)
        return None

    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import opensubtitles  # sibling module - just reusing its pure filename-guessing helper

    # Always parse the filename for season/episode, even when --tmdb-query
    # overrides the title text itself - a "SxxExx" in the name is the same
    # TV-vs-movie signal opensubtitles.search() already keys its own
    # season_number/episode_number params off of, and TMDB needs a
    # completely different pair of endpoints for a series vs. a movie (see
    # search_tv/tv_keywords/tv_genres above - the TV keywords endpoint even
    # uses a different response key than the movie one).
    guess = opensubtitles.guess_query(media)
    is_tv = guess.get("season_number") is not None
    query = (getattr(cfg, "tmdb_query", "") or "").strip() or guess.get("query") or ""
    year = guess.get("year")
    if not query:
        print("  [warn] could not guess a search title for TMDB - set --tmdb-query",
              file=sys.stderr)
        return None

    try:
        match = search_tv(query, year, key) if is_tv else search_movie(query, year, key)
        if not match:
            print(f"  [warn] TMDB: no {'TV' if is_tv else 'movie'} results for {query!r}",
                  file=sys.stderr)
            meta_path.write_text(json.dumps(
                {"query": query, "year": year, "media_type": "tv" if is_tv else "movie",
                 "tmdb_id": None, "faith_based": None},
                indent=2), encoding="utf-8")
            return None
        tmdb_id = match["id"]
        title = match.get("name") if is_tv else match.get("title")
        date = match.get("first_air_date") if is_tv else match.get("release_date")
        keywords = tv_keywords(tmdb_id, key) if is_tv else movie_keywords(tmdb_id, key)
        genres = tv_genres(tmdb_id, key) if is_tv else movie_genres(tmdb_id, key)
        faith = any(_is_faith_tag(k) for k in keywords) or any(_is_faith_tag(g) for g in genres)
        meta = {
            "query": query, "year": year, "media_type": "tv" if is_tv else "movie",
            "tmdb_id": tmdb_id, "title": title, "release_date": date,
            "genres": genres, "keywords": keywords, "faith_based": faith,
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if faith:
            rel_year = (date or "????")[:4]
            kind = "TV series" if is_tv else "film"
            print(f"  TMDB: {title!r} ({rel_year}) {kind} tagged faith-based/Christian - "
                  f"using the stricter profanity_strict/irreverence_strict wordlists")
        return faith
    except TmdbError as exc:
        print(f"  [warn] TMDB lookup failed ({exc}) - continuing without faith-based detection",
              file=sys.stderr)
        return None
