"""Tiny Plex helper: list library sections and trigger a (targeted) scan.

rar2fs breaks Plex's inotify change-detection, so after a DC download lands we
trigger a scan explicitly. A path-scoped refresh is cheaper than a full section
scan.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class Plex:
    def __init__(self, base_url: str, token: str, timeout: int = 20):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> bytes:
        params = dict(params or {})
        params["X-Plex-Token"] = self.token
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            return r.read()

    def sections(self) -> list[dict]:
        root = ET.fromstring(self._get("/library/sections"))
        return [{"key": d.get("key"), "type": d.get("type"), "title": d.get("title")}
                for d in root.findall("Directory")]

    def find_section(self, title: str) -> str | None:
        for s in self.sections():
            if (s["title"] or "").lower() == title.lower():
                return s["key"]
        return None

    def scan(self, section_key: str, path: str | None = None) -> None:
        """Trigger a scan of a section, optionally scoped to a folder path
        (the path must be as PLEX sees it, not the FulDC++ Windows path)."""
        params = {"path": path} if path else None
        self._get(f"/library/sections/{section_key}/refresh", params)
