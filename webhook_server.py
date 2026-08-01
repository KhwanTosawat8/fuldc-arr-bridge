#!/usr/bin/env python3
"""Seerr -> FulDC++ webhook receiver.

On a Seerr "Media Approved" (or auto-approved) notification for a MOVIE, kicks
off a hybrid grab (immediate download or AutoSearch fallback). Stdlib only.

Env: FULDC_URL, FULDC_USER, FULDC_PASS, DC_ROOT, PORT (default 8080),
     MOVIES_ONLY (default "1").
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fuldc_client import FulDCClient
from ranker import Prefs
from core import hybrid_grab, monitor_tv_season

APPROVED = {"MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"}
YEAR_RE = re.compile(r"\((\d{4})\)")


def client() -> FulDCClient:
    return FulDCClient(os.environ.get("FULDC_URL", "http://mgmt:5600"),
                       os.environ.get("FULDC_USER", "peter"),
                       os.environ["FULDC_PASS"])


def parse(payload: dict):
    nt = payload.get("notification_type", "")
    media = payload.get("media") or {}
    mtype = media.get("media_type", "")
    subject = (payload.get("subject") or "").strip()
    m = YEAR_RE.search(subject)
    year = int(m.group(1)) if m else None
    title = (YEAR_RE.sub("", subject).strip(" -") if m else subject).strip()
    return nt, mtype, title, year


def requested_seasons(payload: dict) -> list[int]:
    """Seerr puts requested seasons in the `extra` array as
    {"name": "Requested Seasons", "value": "1, 2"}."""
    for e in payload.get("extra") or []:
        if str(e.get("name", "")).lower().startswith("requested season"):
            return sorted({int(x) for x in re.findall(r"\d+", str(e.get("value", "")))})
    return []


def _prefs() -> Prefs:
    p = Prefs()
    q = os.environ.get("QUALITY", "").strip().lower()
    if q:
        p.require_quality = [q]
        if q not in p.prefer_quality:
            p.prefer_quality = [q] + p.prefer_quality
    return p


def _grab(title, year, *, kind, season=None):
    print(f"[grab] {title!r} ({year}) type={kind}" + (f" S{season:02d}" if season else ""),
          flush=True)
    try:
        res = hybrid_grab(client(), title, year, kind=kind, season=season,
                          prefs=_prefs(), dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                          movies_dir=os.environ.get("MOVIES_DIR"),
                          series_dir=os.environ.get("SERIES_DIR"),
                          log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
    except Exception as e:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r}: {e}", flush=True)


def _monitor_tv(title, season):
    q = os.environ.get("QUALITY", "").strip() or None
    print(f"[grab] {title!r} series S{season:02d} (monitor %[inc])", flush=True)
    try:
        res = monitor_tv_season(client(), title, season,
                                dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                                movies_dir=os.environ.get("MOVIES_DIR"),
                                series_dir=os.environ.get("SERIES_DIR"),
                                quality=q, log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
    except Exception as e:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r} S{season}: {e}", flush=True)


def handle(payload: dict) -> None:
    nt, mtype, title, year = parse(payload)
    if nt not in APPROVED:
        print(f"[skip] notification_type={nt}", flush=True)
        return
    if os.environ.get("MOVIES_ONLY", "1") == "1" and mtype != "movie":
        print(f"[skip] media_type={mtype} (movies only)", flush=True)
        return
    if not title:
        print("[skip] empty title", flush=True)
        return
    if mtype == "tv":
        seasons = requested_seasons(payload)
        if seasons:
            for season in seasons:
                _monitor_tv(title, season)   # %[inc] ongoing-episode monitor
        else:
            _grab(title, year, kind="series")   # no season info -> best-effort grab
    else:
        _grab(title, year, kind="movie")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes = b"ok") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, b"fuldc-arr-bridge webhook up")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}
        self._send(200, b"accepted")
        threading.Thread(target=handle, args=(payload,), daemon=True).start()

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"fuldc-arr-bridge webhook listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
