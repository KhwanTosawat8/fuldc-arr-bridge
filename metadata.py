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
ENDED_STATUSES = {"ended", "canceled", "cancelled"}


def _get_json(url: str, headers: dict | None = None, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _tmdb_details(tmdb_id: int, media_type: str, api_key: str) -> dict:
    kind = "tv" if media_type == "tv" else "movie"
    url = f"{TMDB_BASE}/{kind}/{tmdb_id}?api_key={urllib.parse.quote(api_key)}"
    return _get_json(url)


def _seerr_details(tmdb_id: int, media_type: str, base: str, api_key: str) -> dict:
    kind = "tv" if media_type == "tv" else "movie"
    url = f"{base.rstrip('/')}/api/v1/{kind}/{tmdb_id}"
    return _get_json(url, headers={"X-Api-Key": api_key})


def _details(tmdb_id: int | None, media_type: str, *, log=print) -> dict | None:
    """Fetch a title's metadata (genres + status) from TMDB or Seerr, or None if
    no source is configured / the lookup fails."""
    if not tmdb_id:
        return None
    tmdb_key = os.environ.get("TMDB_API_KEY", "").strip()
    seerr_url = os.environ.get("SEERR_URL", "").strip()
    seerr_key = os.environ.get("SEERR_API_KEY", "").strip()
    try:
        if tmdb_key:
            return _tmdb_details(tmdb_id, media_type, tmdb_key)
        if seerr_url and seerr_key:
            return _seerr_details(tmdb_id, media_type, seerr_url, seerr_key)
    except Exception as e:  # noqa: BLE001 - metadata is best-effort, never fatal
        log(f"# metadata lookup failed for {media_type} {tmdb_id}: {e}")
    return None


def genres_for(tmdb_id: int | None, media_type: str, *, log=print) -> list[str] | None:
    """Return the title's genre names, or None if unavailable."""
    d = _details(tmdb_id, media_type, log=log)
    return None if d is None else [g.get("name", "") for g in d.get("genres", [])]


def _kids_genre_set() -> set[str]:
    raw = os.environ.get("KIDS_GENRES", "").strip()
    if not raw:
        return set(DEFAULT_KIDS_GENRES)
    return {g.strip().lower() for g in raw.split(",") if g.strip()}


def classify(tmdb_id: int | None, media_type: str, *, log=print) -> tuple[bool, bool]:
    """Return (is_kids, is_ended) from a single metadata lookup.

    is_kids: genres include a configured kids genre (default Kids/Family;
             'Animation' alone is NOT kids).
    is_ended: TV show whose status is Ended/Canceled — such shows should be
              grabbed as season packs, not monitored per-episode with %[inc].
    """
    d = _details(tmdb_id, media_type, log=log)
    if not d:
        return False, False
    genres = [g.get("name", "") for g in d.get("genres", [])]
    kids = any(name.lower() in _kids_genre_set() for name in genres)
    ended = (media_type == "tv"
             and (d.get("status") or "").strip().lower() in ENDED_STATUSES)
    return kids, ended


def is_kids(tmdb_id: int | None, media_type: str, *, log=print) -> bool:
    return classify(tmdb_id, media_type, log=log)[0]
