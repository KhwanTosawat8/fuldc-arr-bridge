"""Torznab indexer face — lets Radarr/Sonarr search Direct Connect via FulDC++.

Radarr/Sonarr (or Prowlarr) add this as a "Generic Torznab" indexer. On a search
we run a FulDC++ hub search, rank the results, and return them as a Torznab XML
feed. Each release's download link is a synthetic magnet whose btih maps back to
the DC result (see store.py) so the qBittorrent shim can fetch it later.
"""

from __future__ import annotations

import urllib.parse
from email.utils import formatdate
from xml.sax.saxutils import escape

from fuldc_client import FulDCClient
from ranker import Prefs, rank
from core import run_search
import store

MOVIE_CAT = 2000
TV_CAT = 5000
TORZNAB_NS = "http://torznab.com/schemas/2015/feed"


def caps_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="fuldc-arr-bridge"/>
  <limits max="100" default="50"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <movie-search available="yes" supportedParams="q,imdbid,tmdbid"/>
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
                 season: int | None, limit: int, prefs: Prefs) -> list[dict]:
    """Run a DC search, rank, remember each result, and return Torznab items."""
    if not query.strip():
        return []   # RSS sync with no query — nothing to search on DC
    iid, results = run_search(client, query, None, wait=8, kind=kind, season=season)
    cands = rank(results, query, None, prefs, kind=kind)
    if iid is not None:
        client.close(iid)

    items = []
    for c in cands[:limit]:
        r = c.result
        size = int(r.get("size") or 0)
        h = store.synthetic_hash(r)
        store.put(h, {
            "pattern": query, "kind": kind, "season": season,
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
            "pubdate": formatdate(r.get("time") or None, usegmt=True),
        })
    return items


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
            f'<torznab:attr name="category" value="{it["cat"]}"/>',
            f'<torznab:attr name="seeders" value="{seeders}"/>',
            f'<torznab:attr name="peers" value="{seeders}"/>',
            '<torznab:attr name="downloadvolumefactor" value="0"/>',
            '<torznab:attr name="uploadvolumefactor" value="1"/>',
            "</item>",
        ]
    parts += ["</channel>", "</rss>"]
    return "\n".join(parts)
