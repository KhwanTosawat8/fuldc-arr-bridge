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
from metadata import classify

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
    try:
        tmdb = int(media.get("tmdbId")) if media.get("tmdbId") not in (None, "") else None
    except (TypeError, ValueError):
        tmdb = None
    return nt, mtype, title, year, tmdb


def _request_dirs(kids: bool):
    """Return (movies_dir, series_dir) for this request. Kids content goes to
    dedicated folders (default <DC_ROOT>\\kids.movies / kids.series)."""
    if kids:
        root = os.environ.get("DC_ROOT", "S:\\dc").rstrip("\\/")
        mov = os.environ.get("KIDS_MOVIES_DIR") or f"{root}\\kids.movies"
        ser = os.environ.get("KIDS_SERIES_DIR") or f"{root}\\kids.series"
        return mov, ser
    return os.environ.get("MOVIES_DIR"), os.environ.get("SERIES_DIR")


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


def _grab(title, year, *, kind, season=None, movies_dir=None, series_dir=None):
    print(f"[grab] {title!r} ({year}) type={kind}" + (f" S{season:02d}" if season else ""),
          flush=True)
    try:
        res = hybrid_grab(client(), title, year, kind=kind, season=season,
                          prefs=_prefs(), dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                          movies_dir=movies_dir, series_dir=series_dir,
                          log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
    except Exception as e:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r}: {e}", flush=True)


def _monitor_tv(title, season, *, series_dir=None):
    q = os.environ.get("QUALITY", "").strip() or None
    print(f"[grab] {title!r} series S{season:02d} (monitor %[inc])", flush=True)
    try:
        res = monitor_tv_season(client(), title, season,
                                dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                                movies_dir=os.environ.get("MOVIES_DIR"),
                                series_dir=series_dir,
                                quality=q, log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
    except Exception as e:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r} S{season}: {e}", flush=True)


def handle(payload: dict) -> None:
    nt, mtype, title, year, tmdb = parse(payload)
    if nt not in APPROVED:
        print(f"[skip] notification_type={nt}", flush=True)
        return
    if os.environ.get("MOVIES_ONLY", "1") == "1" and mtype != "movie":
        print(f"[skip] media_type={mtype} (movies only)", flush=True)
        return
    if not title:
        print("[skip] empty title", flush=True)
        return
    kids, ended = classify(tmdb, mtype, log=lambda m: print(m, flush=True))
    if os.environ.get("KIDS_ROUTING", "1") != "1":
        kids = False
    mov_dir, ser_dir = _request_dirs(kids)
    if kids:
        print(f"[kids] routing {title!r} -> kids folders", flush=True)
    if mtype == "tv":
        seasons = requested_seasons(payload)
        if ended:
            # Ended/canceled show: episodes are already out (usually as season
            # packs). Grab each requested season as a pack instead of a %[inc]
            # per-episode monitor that would never find anything.
            print(f"[ended] {title!r} -> season-pack grab (no %[inc] monitor)", flush=True)
            for season in (seasons or [None]):
                _grab(title, year, kind="series", season=season, series_dir=ser_dir)
        elif seasons:
            for season in seasons:
                _monitor_tv(title, season, series_dir=ser_dir)   # %[inc] monitor
        else:
            _grab(title, year, kind="series", series_dir=ser_dir)
    else:
        _grab(title, year, kind="movie", movies_dir=mov_dir)


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
