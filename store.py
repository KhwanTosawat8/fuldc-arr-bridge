"""In-memory map: synthetic release hash -> re-acquisition info.

The Torznab indexer hands Radarr/Sonarr a magnet whose btih is a synthetic hash
of a DC search result. Later, the qBittorrent shim receives that magnet and must
re-find and download the same release. DC results are ephemeral, so we remember
what each hash pointed at.

In-memory (stateless, no volume). If the process restarts, mappings are lost but
Radarr/Sonarr simply re-query on their next RSS/search cycle, so it self-heals.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

_MAX = 5000
_store: "OrderedDict[str, dict]" = OrderedDict()


def synthetic_hash(result: dict) -> str:
    """Stable 40-hex id for a grouped search result. Files have a TTH; directory
    releases don't, so fall back to path + exact size."""
    tth = (result.get("tth") or "").strip()
    key = tth if tth else f"{result.get('path','')}|{result.get('size')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def put(h: str, info: dict) -> None:
    _store[h] = info
    _store.move_to_end(h)
    while len(_store) > _MAX:
        _store.popitem(last=False)


def get(h: str) -> dict | None:
    return _store.get(h)
