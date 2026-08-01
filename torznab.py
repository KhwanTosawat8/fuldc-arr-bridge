"""Torznab indexer face — lets Radarr/Sonarr search Direct Connect via FulDC++.

Radarr/Sonarr (or Prowlarr) add this as a "Generic Torznab" indexer. On a search
we run a FulDC++ hub search, rank the results, and return them as a Torznab XML
feed. Each release's download link is a synthetic magnet whose btih maps back to
the DC result (see store.py) so the qBittorrent shim can fetch it later.
"""

from __future__ import annotations

import hashlib
import os
import urllib.parse
from email.utils import formatdate
from xml.sax.saxutils import escape

from fuldc_client import PRIO_LOW, FulDCClient
from ranker import Prefs, rank
from core import searched
import store

MOVIE_CAT = 2000
TV_CAT = 5000
TORZNAB_NS = "http://torznab.com/schemas/2015/feed"


def caps_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="fuldc-arr-bridge"/>
  <limits max="100" default="50"/>
  <!-- Only advertise what we actually implement: DC has no id-based lookup, so
       claiming imdbid/tmdbid/tvdbid makes Radarr/Sonarr send id-only searches
       (empty q) that can never return a result. -->
  <searching>
    <search available="yes" supportedParams="q"/>
    <movie-search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep"/>
  </searching>
  <categories>
    <category id="{MOVIE_CAT}" name="Movies"/>
    <category id="{TV_CAT}" name="TV"/>
  </categories>
</caps>"""


def _magnet(h: str, title: str, size: int) -> str:
    dn = urllib.parse.quote(title)
    return f"magnet:?xt=urn:btih:{h}&dn={dn}&xl={size}"


def search_items(client: FulDCClient, *, query: str, kind: str,
                 season: int | None, limit: int, prefs: Prefs,
                 wait: float = 8.0) -> list[dict]:
    """Run a DC search, rank, remember each result, and return Torznab items.

    An empty query is Radarr/Sonarr's RSS request — and their indexer Test is
    exactly one of those, with `releases.Empty()` treated as a hard
    ValidationFailure. Returning [] therefore made the indexer impossible to
    add at all. DC has no "recent releases" feed to answer it honestly, so fall
    back to a configured probe term (RSS_PROBE, default "1080p") purely so the
    Test has something real to chew on.
    """
    if not query.strip():
        query = os.environ.get("RSS_PROBE", "1080p").strip()
        if not query:
            return []
    # Indexer traffic is overwhelmingly Radarr/Sonarr's periodic RSS sync rather
    # than a person waiting, so search at background priority — these are the
    # ones that *should* be dropped when the client's search queue is loaded.
    with searched(client, query, None, wait=wait, kind=kind, season=season,
                  priority=PRIO_LOW) as (_iid, results):
        cands = rank(results, query, None, prefs, kind=kind)

    items = []
    for c in cands[:limit]:
        r = c.result
        size = int(r.get("size") or 0)
        h = store.synthetic_hash(r)
        store.put(h, {
            # `q` on a tvsearch is the bare show title — remember it so the
            # download client files the release under the right folder
            "pattern": query, "show": query, "kind": kind, "season": season,
            "release": c.release, "tth": r.get("tth") or "",
            "path": r.get("path") or "", "size": size,
        })
        items.append({
            "title": c.release,
            "guid": h,
            "size": size,
            "magnet": _magnet(h, c.release, size),
            "cat": TV_CAT if kind == "series" else MOVIE_CAT,
            "seeders": (r.get("users") or {}).get("count", 0),
            "pubdate": _pubdate(r, h),
            "infohash": h,
        })
    return items


def _pubdate(result: dict, h: str) -> str:
    """A stable RFC-822 date for this release.

    Radarr matches delay-profile pending releases on title+pubDate+indexer, so
    a pubDate that changes between searches produces duplicate pending entries
    and repeated grabs. `now` was therefore the one value we must not use as a
    fallback. Derive a fixed pseudo-date from the release hash instead, and
    treat an implausible timestamp as absent: a millisecond value would render
    a year-50000 date, and RssParser rejects the *entire feed* when a pubDate
    fails to parse.
    """
    ts = result.get("time") or 0
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        ts = 0
    if ts > 4102444800:          # > year 2100, almost certainly milliseconds
        ts //= 1000
    if not 946684800 < ts < 4102444800:   # outside 2000..2100 -> not a date
        # Deterministic, stable, and safely in the past.
        ts = 1262304000 + int(hashlib.sha1(h.encode()).hexdigest()[:6], 16)
    return formatdate(ts, usegmt=True)


def feed_xml(items: list[dict]) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<rss version="2.0" xmlns:torznab="{TORZNAB_NS}">',
        "<channel>", "<title>fuldc-arr-bridge</title>",
    ]
    for it in items:
        seeders = it["seeders"]
        parts += [
            "<item>",
            f"<title>{escape(it['title'])}</title>",
            f'<guid isPermaLink="false">{it["guid"]}</guid>',
            f"<size>{it['size']}</size>",
            f"<pubDate>{it['pubdate']}</pubDate>",
            f'<link>{escape(it["magnet"])}</link>',
            f'<enclosure url="{escape(it["magnet"])}" length="{it["size"]}" '
            'type="application/x-bittorrent"/>',
            # A bare <size> element is NOT parsed by TorznabRssParser; it reads
            # the torznab attr first and falls back to enclosure/@length.
            f'<torznab:attr name="size" value="{it["size"]}"/>',
            # Blocklisting and "Blocklist and Search" key on infohash first.
            f'<torznab:attr name="infohash" value="{it["infohash"]}"/>',
            f'<torznab:attr name="magneturl" value="{escape(it["magnet"])}"/>',
            f'<torznab:attr name="category" value="{it["cat"]}"/>',
            f'<torznab:attr name="seeders" value="{seeders}"/>',
            f'<torznab:attr name="peers" value="{seeders}"/>',
            '<torznab:attr name="downloadvolumefactor" value="0"/>',
            '<torznab:attr name="uploadvolumefactor" value="1"/>',
            "</item>",
        ]
    parts += ["</channel>", "</rss>"]
    return "\n".join(parts)
