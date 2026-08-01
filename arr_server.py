#!/usr/bin/env python3
"""Radarr/Sonarr integration server for fuldc-arr-bridge.

Serves two faces on one port (default 9117):
  * Torznab indexer   -> /torznab/api   (search Direct Connect via FulDC++)
  * qBittorrent shim   -> /api/v2/...     (download client; added next)

Radarr/Sonarr config:
  Indexer (Generic Torznab):  URL http://<host>:9117/torznab   API key = TORZNAB_APIKEY
  Download client (qBittorrent): host <host>, port 9117

Env: FULDC_URL, FULDC_USER, FULDC_PASS, DC_ROOT, MOVIES_DIR, SERIES_DIR,
     TORZNAB_APIKEY, ARR_PORT (default 9117).
"""

from __future__ import annotations

import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fuldc_client import FulDCClient
from ranker import Prefs
import torznab


def client() -> FulDCClient:
    return FulDCClient(os.environ.get("FULDC_URL", "http://mgmt:5600"),
                       os.environ.get("FULDC_USER", "admin"),
                       os.environ["FULDC_PASS"])


def _apikey_ok(params: dict) -> bool:
    want = os.environ.get("TORZNAB_APIKEY", "")
    if not want:
        return True   # no key configured = open (dev only)
    return params.get("apikey", [""])[0] == want


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/xml; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/torznab/api", "/api"):
            return self._torznab(params)
        if path in ("", "/", "/health"):
            return self._send(200, b"fuldc-arr-bridge arr server up", "text/plain")
        self._send(404, b"not found", "text/plain")

    def _torznab(self, params: dict):
        if not _apikey_ok(params):
            return self._send(401, b'<error code="100" description="Incorrect API key"/>')
        t = params.get("t", ["search"])[0]
        if t == "caps":
            return self._send(200, torznab.caps_xml().encode())

        q = params.get("q", [""])[0]
        cats = params.get("cat", [""])[0]
        kind = "series" if (t == "tvsearch" or str(torznab.TV_CAT) in cats) else "movie"
        season = None
        if params.get("season", [""])[0].isdigit():
            season = int(params["season"][0])
        limit = int(params.get("limit", ["50"])[0])
        try:
            items = torznab.search_items(client(), query=q, kind=kind,
                                         season=season, limit=limit, prefs=Prefs())
            print(f"[torznab] t={t} q={q!r} kind={kind} season={season} -> {len(items)} items", flush=True)
            self._send(200, torznab.feed_xml(items).encode())
        except Exception as e:  # noqa: BLE001
            print(f"[torznab] error: {e}", flush=True)
            self._send(200, torznab.feed_xml([]).encode())

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("ARR_PORT", "9117"))
    print(f"fuldc-arr-bridge arr server listening on :{port} "
          f"(torznab /torznab/api, qbit /api/v2)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
