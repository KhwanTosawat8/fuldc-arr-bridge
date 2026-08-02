"""Auto new-season detection.

Sonarr's one job that the Seerr flow doesn't cover on its own: when a show you
already follow gets a *new* season, start grabbing it without a fresh request.

The existing `%[inc]` AutoSearch items are the source of truth for what you
follow. For each, we look up the show on TMDB/Seerr and, if a season newer than
the highest one you monitor has started airing, we create a `%[inc]` monitor for
it — into the same folder, same quality. Additive only; nothing is removed.

Needs a metadata source (TMDB_API_KEY or SEERR_URL+SEERR_API_KEY); a no-op
without one.
"""

from __future__ import annotations

import re

from fuldc_client import FulDCClient
from metadata import aired_seasons, find_tv_id
from core import monitor_tv_season

# capture the series root (series or kids.series), the show folder and season
_TARGET = re.compile(
    r"^(?P<root>.*\\(?:kids\.series|series))\\(?P<folder>[^\\]+)\\S(?P<season>\d{1,2})\\?$",
    re.I,
)
_YEAR = re.compile(r"\.(\d{4})$")


def _quality(search_string: str) -> str | None:
    """Trailing quality token after the %[inc] placeholder, if any."""
    parts = search_string.split("%[inc]", 1)
    return (parts[1].strip() or None) if len(parts) == 2 else None


def _monitored(client: FulDCClient) -> dict[tuple[str, str], dict]:
    """Group the live %[inc] monitors by (series-root, show-folder)."""
    shows: dict[tuple[str, str], dict] = {}
    for it in client.list_autosearch():
        ss = it.get("search_string") or ""
        if "%[inc]" not in ss:
            continue
        tgt = it.get("target")
        tp = tgt.get("path") if isinstance(tgt, dict) else tgt
        m = _TARGET.search(tp or "")
        if not m:
            continue
        key = (m.group("root"), m.group("folder"))
        d = shows.setdefault(key, {"seasons": set(), "quality": None})
        d["seasons"].add(int(m.group("season")))
        d["quality"] = d["quality"] or _quality(ss)
    return shows


def sweep(client: FulDCClient, *, dc_root: str = "S:\\dc",
          movies_dir: str | None = None, log=print) -> int:
    """Add a %[inc] monitor for every newly-aired season beyond the highest one
    already followed. Returns the number of monitors created."""
    added = 0
    shows = _monitored(client)
    for (root, folder), d in shows.items():
        ym = _YEAR.search(folder)
        year = int(ym.group(1)) if ym else None
        base = folder[: ym.start()] if ym else folder
        query = base.replace(".", " ").strip()
        tid = find_tv_id(query, log=log)
        if not tid:
            log(f"# [season] no TMDB match for {query!r} — skipping")
            continue
        newest = max(d["seasons"])
        new_seasons = sorted(s for s in aired_seasons(tid, log=log) if s > newest)
        for s in new_seasons:
            monitor_tv_season(client, base, s, year=year, dc_root=dc_root,
                              movies_dir=movies_dir, series_dir=root,
                              quality=d["quality"], log=log)
            log(f"# [season] NEW: {query} S{s:02d} -> monitor created")
            added += 1
    log(f"# [season] sweep done: {added} new-season monitor(s) across {len(shows)} shows")
    return added
