"""Shared grab logic used by both the CLI and the Seerr webhook service.

Hybrid strategy:
  * content available now  -> download the ranked-best release immediately
  * nothing shared now     -> create a persistent AutoSearch item so FulDC++
                              downloads it natively once it appears (no
                              bridge-side retry loop)
"""

from __future__ import annotations

from fuldc_client import FulDCClient
from ranker import Prefs, rank, search_queries, strip_leading_article

BAD_SOURCE = "cam camrip ts telesync tc telecine hdcam screener sample workprint"


def resolve_target(kind: str, title: str, series: str | None,
                   dc_root: str = "S:\\dc", explicit: str | None = None,
                   season: int | None = None) -> str:
    if explicit:
        return explicit
    root = dc_root.rstrip("\\/")
    if kind == "series":
        show = (series or title).strip()
        base = f"{root}\\series\\{show}\\"
        return base + f"S{season:02d}\\" if season else base
    return f"{root}\\movies\\"


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
                dc_root: str = "S:\\dc", target: str | None = None,
                wait: float = 10.0, log=print) -> dict:
    prefs = prefs or Prefs()
    target = resolve_target(kind, title, series, dc_root, target, season)
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
    # nothing available now -> persistent AutoSearch
    matcher = autosearch_matcher(title, year, kind, season)
    item = client.create_autosearch(matcher, target_directory=target, excluded=BAD_SOURCE)
    return {"mode": "autosearch", "matcher": matcher,
            "autosearch_id": item.get("id"), "target": target, "season": season}
