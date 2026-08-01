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
import threading
from collections import OrderedDict

_MAX = 5000
_store: "OrderedDict[str, dict]" = OrderedDict()
# Written from Torznab search threads, read from qBittorrent add threads.
_lock = threading.Lock()


def synthetic_hash(result: dict) -> str:
    """Stable 40-hex id for a grouped search result. Files have a TTH; directory
    releases don't, so fall back to path + exact size."""
    tth = (result.get("tth") or "").strip()
    key = tth if tth else f"{result.get('path','')}|{result.get('size')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def put(h: str, info: dict) -> None:
    with _lock:
        _store[h] = info
        _store.move_to_end(h)
        while len(_store) > _MAX:
            _store.popitem(last=False)


def get(h: str) -> dict | None:
    """Look up a release, promoting it to the newest end.

    Without the promotion this is insertion-ordered, not least-recently-used:
    a couple of indexers RSS-syncing every 15 minutes evict the map within
    hours, so a release Radarr found in the morning fails as "unknown magnet"
    when the user grabs it that evening, with no restart involved."""
    with _lock:
        info = _store.get(h)
        if info is not None:
            _store.move_to_end(h)
        return info
