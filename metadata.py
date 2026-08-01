"""Optional metadata lookup used to route kids content to dedicated folders.

Determines a title's genres from either TMDB directly (TMDB_API_KEY) or the
user's own Seerr/Jellyseerr/Overseerr instance (SEERR_URL + SEERR_API_KEY),
so an approved kids show/movie can be sent to kids.series / kids.movies instead
of the normal series / movies folders.

Stdlib only. If no source is configured the classifier simply returns False and
routing behaves exactly as before.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

TMDB_BASE = "https://api.themoviedb.org/3"
DEFAULT_KIDS_GENRES = {"kids", "family"}


def _get_json(url: str, headers: dict | None = None, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _tmdb_genres(tmdb_id: int, media_type: str, api_key: str) -> list[str]:
    kind = "tv" if media_type == "tv" else "movie"
    url = f"{TMDB_BASE}/{kind}/{tmdb_id}?api_key={urllib.parse.quote(api_key)}"
    data = _get_json(url)
    return [g.get("name", "") for g in data.get("genres", [])]


def _seerr_genres(tmdb_id: int, media_type: str, base: str, api_key: str) -> list[str]:
    kind = "tv" if media_type == "tv" else "movie"
    url = f"{base.rstrip('/')}/api/v1/{kind}/{tmdb_id}"
    data = _get_json(url, headers={"X-Api-Key": api_key})
    return [g.get("name", "") for g in data.get("genres", [])]


def genres_for(tmdb_id: int | None, media_type: str, *, log=print) -> list[str] | None:
    """Return the title's genre names, or None if no source is configured or the
    lookup fails (callers treat None/[] as 'not kids')."""
    if not tmdb_id:
        return None
    tmdb_key = os.environ.get("TMDB_API_KEY", "").strip()
    seerr_url = os.environ.get("SEERR_URL", "").strip()
    seerr_key = os.environ.get("SEERR_API_KEY", "").strip()
    try:
        if tmdb_key:
            return _tmdb_genres(tmdb_id, media_type, tmdb_key)
        if seerr_url and seerr_key:
            return _seerr_genres(tmdb_id, media_type, seerr_url, seerr_key)
    except Exception as e:  # noqa: BLE001 - metadata is best-effort, never fatal
        log(f"# genre lookup failed for {media_type} {tmdb_id}: {e}")
    return None


def _kids_genre_set() -> set[str]:
    raw = os.environ.get("KIDS_GENRES", "").strip()
    if not raw:
        return set(DEFAULT_KIDS_GENRES)
    return {g.strip().lower() for g in raw.split(",") if g.strip()}


def is_kids(tmdb_id: int | None, media_type: str, *, log=print) -> bool:
    """True if the title's genres include a configured kids genre (default
    Kids/Family). 'Animation' is deliberately NOT a kids signal on its own."""
    genres = genres_for(tmdb_id, media_type, log=log)
    if not genres:
        return False
    wanted = _kids_genre_set()
    return any(name.lower() in wanted for name in genres)
