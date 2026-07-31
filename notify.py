"""Optional, pluggable post-download library refresh.

The bridge core is media-server-agnostic — it only drops the release into the
share folder. This module is a *convenience accelerator* so the operator's own
Seerr flips to "Available" faster; it is not required (Seerr/most servers rescan
periodically). Backend chosen via MEDIASERVER env: plex | jellyfin | webhook | none.
"""

from __future__ import annotations

import json
import os
import urllib.request


def refresh(kind: str) -> None:
    backend = os.environ.get("MEDIASERVER", "none").lower()
    if backend == "plex":
        _plex(kind)
    elif backend == "jellyfin":
        _jellyfin()
    elif backend == "webhook":
        _webhook(kind)
    else:
        print("# MEDIASERVER not set (plex|jellyfin|webhook) — skipping library "
              "refresh; your server/Seerr will pick it up on its next scan")


def _plex(kind: str) -> None:
    from plex import Plex
    url, tok = os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN")
    if not (url and tok):
        print("# PLEX_URL/PLEX_TOKEN not set — skipping Plex scan")
        return
    p = Plex(url, tok)
    section = (os.environ.get("PLEX_MOVIES_SECTION", "Movies") if kind == "movie"
               else os.environ.get("PLEX_SERIES_SECTION", "TV Shows"))
    key = p.find_section(section)
    if not key:
        print(f"# Plex section {section!r} not found; have: {[s['title'] for s in p.sections()]}")
        return
    p.scan(key)
    print(f"# triggered Plex scan of section {section!r}")


def _jellyfin() -> None:
    url, tok = os.environ.get("JELLYFIN_URL"), os.environ.get("JELLYFIN_TOKEN")
    if not (url and tok):
        print("# JELLYFIN_URL/JELLYFIN_TOKEN not set — skipping Jellyfin refresh")
        return
    req = urllib.request.Request(url.rstrip("/") + "/Library/Refresh", method="POST",
                                 headers={"X-Emby-Token": tok})
    urllib.request.urlopen(req, timeout=20)
    print("# triggered Jellyfin library refresh")


def _webhook(kind: str) -> None:
    hook = os.environ.get("NOTIFY_WEBHOOK")
    if not hook:
        print("# NOTIFY_WEBHOOK not set — skipping")
        return
    data = json.dumps({"event": "download_complete", "kind": kind}).encode()
    req = urllib.request.Request(hook, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20)
    print(f"# posted completion webhook to {hook}")
