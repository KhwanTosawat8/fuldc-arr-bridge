"""Shared grab logic used by both the CLI and the Seerr webhook service.

Hybrid strategy:
  * content available now  -> download the ranked-best release immediately
  * nothing shared now     -> create a persistent AutoSearch item so FulDC++
                              downloads it natively once it appears (no
                              bridge-side retry loop)
"""

from __future__ import annotations

import re

from fuldc_client import FulDCClient
from ranker import Prefs, rank, search_queries, strip_leading_article

BAD_SOURCE = "cam camrip ts telesync tc telecine hdcam screener sample workprint"

# characters Windows forbids in a path component, plus control chars
_UNSAFE_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_component(name: str) -> str:
    """Make an untrusted string safe to use as ONE folder name.

    The show name comes straight off the Seerr webhook, i.e. off the network —
    without this, a subject like '..\\..\\Users\\Public (2020)' would walk the
    download target out of DC_ROOT and write anywhere on the FulDC++ host.
    Stripping the separators kills traversal; stripping leading/trailing dots
    kills the '..' and '.' components themselves.
    """
    cleaned = _UNSAFE_COMPONENT.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120].strip() or "unknown"


def resolve_target(kind: str, title: str, series: str | None,
                   dc_root: str = "S:\\dc", explicit: str | None = None,
                   season: int | None = None, movies_dir: str | None = None,
                   series_dir: str | None = None) -> str:
    """Windows target path on the FulDC++ host. Users set their own share root
    via DC_ROOT (e.g. D:\\Media), or override the movie/TV folders directly with
    MOVIES_DIR / SERIES_DIR for non-standard layouts.

    dc_root / movies_dir / series_dir / explicit are operator config and are
    trusted; the show name is not (see safe_component)."""
    if explicit:
        return explicit
    root = dc_root.rstrip("\\/")
    if kind == "series":
        base = (series_dir or f"{root}\\series").rstrip("\\/")
        show = safe_component(series or title)
        p = f"{base}\\{show}\\"
        return p + f"S{season:02d}\\" if season else p
    md = (movies_dir or f"{root}\\movies").rstrip("\\/")
    return f"{md}\\"


def _queries(title: str, year: int | None, kind: str, season: int | None) -> list[str]:
    if kind == "series" and season:
        base = strip_leading_article(title)
        return [f"{base} S{season:02d}", f"{base} S{season}"]
    return search_queries(title, year)


def run_search(client: FulDCClient, title: str, year: int | None,
               wait: float = 10.0, log=print, kind: str = "movie",
               season: int | None = None):
    """Try fallback queries until one returns results. Returns (iid, results);
    iid may be None. Closes instances that yielded nothing."""
    for q in _queries(title, year, kind, season):
        log(f"# search {q!r}")
        iid, results = client.search(q, wait=wait)
        if results:
            return iid, results
        client.close(iid)
    return None, []


def autosearch_matcher(title: str, year: int | None, kind: str = "movie",
                       season: int | None = None) -> str:
    base = strip_leading_article(title)
    if kind == "series" and season:
        return f"{base} S{season:02d}"
    return f"{base} {year}" if year else base


def hybrid_grab(client: FulDCClient, title: str, year: int | None, *,
                kind: str = "movie", series: str | None = None,
                season: int | None = None, prefs: Prefs | None = None,
                dc_root: str = "S:\\dc", movies_dir: str | None = None,
                series_dir: str | None = None, target: str | None = None,
                wait: float = 10.0, log=print) -> dict:
    prefs = prefs or Prefs()
    target = resolve_target(kind, title, series, dc_root, target, season,
                            movies_dir, series_dir)
    iid, results = run_search(client, title, year, wait, log, kind, season)
    if results:
        cands = rank(results, title, year, prefs, kind=kind)
        if cands:
            best = cands[0]
            info = client.download_result(iid, best.result["id"], target, name=best.release)
            client.close(iid)
            return {"mode": "download", "release": best.release, "score": best.score,
                    "bundle_id": info.get("bundle_id"), "target": target, "season": season}
        client.close(iid)
    elif iid is not None:
        client.close(iid)
    # nothing available now -> persistent AutoSearch. Bake the required quality
    # into the search string so FulDC++ only grabs matching releases (the
    # server-side AutoSearch can't reuse the ranker's quality filter).
    matcher = autosearch_matcher(title, year, kind, season)
    if prefs.require_quality:
        matcher = f"{matcher} {prefs.require_quality[0]}"
    item = client.create_autosearch(matcher, target_directory=target, excluded=BAD_SOURCE)
    return {"mode": "autosearch", "matcher": matcher,
            "autosearch_id": item.get("id"), "target": target, "season": season}


def grab_tv_season(client: FulDCClient, show: str, season: int, *,
                   prefs: Prefs | None = None, dc_root: str = "S:\\dc",
                   movies_dir: str | None = None, series_dir: str | None = None,
                   quality: str | None = None, wait: float = 10.0,
                   log=print) -> dict:
    """Season request: grab a season pack now if one is shared, otherwise fall
    back to the %[inc] per-episode monitor.

    The monitor alone never picks up an already-complete older season until its
    next scheduled run, and it skips the season-pack preference in the ranker —
    so try a real search first, exactly like the movie path does."""
    prefs = prefs or Prefs()
    target = resolve_target("series", show, None, dc_root, None, season,
                            movies_dir, series_dir)
    iid, results = run_search(client, show, None, wait, log, "series", season)
    if results:
        cands = rank(results, show, None, prefs, kind="series")
        if cands:
            best = cands[0]
            info = client.download_result(iid, best.result["id"], target, name=best.release)
            client.close(iid)
            log(f"# season pack {best.release!r} -> {target}")
            return {"mode": "download", "release": best.release, "score": best.score,
                    "bundle_id": info.get("bundle_id"), "target": target, "season": season}
        client.close(iid)
    elif iid is not None:
        client.close(iid)
    return monitor_tv_season(client, show, season, dc_root=dc_root,
                             movies_dir=movies_dir, series_dir=series_dir,
                             quality=quality, log=log)


def monitor_tv_season(client: FulDCClient, show: str, season: int, *,
                      dc_root: str = "S:\\dc", movies_dir: str | None = None,
                      series_dir: str | None = None, quality: str | None = None,
                      log=print) -> dict:
    """Create a persistent per-episode AutoSearch for an ongoing season using the
    AirDC++ %[inc] increment token — grabs each episode as it appears (existing
    and future). This is the Sonarr-style 'monitor an airing show' behavior,
    done natively by FulDC++. remove_after_hit stays False so it keeps going.
    """
    target = resolve_target("series", show, None, dc_root, None, season,
                            movies_dir, series_dir)
    base = strip_leading_article(show)
    q = f" {quality}" if quality else ""
    matcher = f"{base} S{season:02d}E%[inc]{q}"
    item = client.create_autosearch(matcher, target_directory=target,
                                    excluded=BAD_SOURCE, remove_after_hit=False)
    log(f"# monitor {matcher!r} -> {target}")
    return {"mode": "monitor", "matcher": matcher,
            "autosearch_id": item.get("id"), "target": target, "season": season}
