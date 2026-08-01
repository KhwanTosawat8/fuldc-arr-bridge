#!/usr/bin/env python3
"""Seerr -> FulDC++ webhook receiver.

On a Seerr "Media Approved" (or auto-approved) notification for a MOVIE, kicks
off a hybrid grab (immediate download or AutoSearch fallback). Stdlib only.

Env: FULDC_URL, FULDC_USER, FULDC_PASS, DC_ROOT, PORT (default 8080),
     MOVIES_ONLY (default "0"), WEBHOOK_TOKEN (optional shared secret),
     MEDIASERVER (optional post-download library refresh).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fuldc_client import FulDCClient
from ranker import Prefs
from core import grab_tv_season, hybrid_grab
from metadata import classify

APPROVED = {"MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"}
YEAR_RE = re.compile(r"\((\d{4})\)")


def client() -> FulDCClient:
    return FulDCClient(os.environ.get("FULDC_URL", "http://host.docker.internal:5600"),
                       os.environ.get("FULDC_USER", "admin"),
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


def _after_download(c: FulDCClient, res: dict, kind: str) -> None:
    """If the operator configured a media server, wait for the bundle and poke it
    so Seerr flips to Available without waiting for the next periodic scan."""
    if res.get("mode") != "download" or not res.get("bundle_id"):
        return
    if os.environ.get("MEDIASERVER", "none").lower() in ("", "none"):
        return
    final = c.wait_bundle(res["bundle_id"])
    fsid = (final or {}).get("status", {}).get("id")
    print(f"[bundle] {res['bundle_id']} final status: {fsid}", flush=True)
    if fsid in c.DONE_ON_DISK:
        from notify import refresh
        refresh(kind)


def _grab(title, year, *, kind, season=None, movies_dir=None, series_dir=None):
    print(f"[grab] {title!r} ({year}) type={kind}" + (f" S{season:02d}" if season else ""),
          flush=True)
    try:
        c = client()
        res = hybrid_grab(c, title, year, kind=kind, season=season,
                          prefs=_prefs(), dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                          movies_dir=movies_dir, series_dir=series_dir,
                          log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
        _after_download(c, res, kind)
    except Exception as e:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r}: {e}", flush=True)


def _grab_season(title, season, *, series_dir=None, year=None):
    q = os.environ.get("QUALITY", "").strip() or None
    print(f"[grab] {title!r} series S{season:02d}", flush=True)
    try:
        c = client()
        res = grab_tv_season(c, title, season, year=year, prefs=_prefs(),
                             dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                             movies_dir=os.environ.get("MOVIES_DIR"),
                             series_dir=series_dir,
                             quality=q, log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
        _after_download(c, res, "series")
    except Exception as e:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r} S{season}: {e}", flush=True)


def handle(payload: dict) -> None:
    nt, mtype, title, year, tmdb = parse(payload)
    if nt not in APPROVED:
        print(f"[skip] notification_type={nt}", flush=True)
        return
    if os.environ.get("MOVIES_ONLY", "0") == "1" and mtype != "movie":
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
                _grab_season(title, season, series_dir=ser_dir, year=year)   # pack now, else %[inc] monitor
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

    def _authorized(self) -> bool:
        """Optional shared secret. This endpoint queues downloads on your box,
        so if WEBHOOK_TOKEN is set we require it as ?token=… or X-Webhook-Token.
        Unset = open (LAN-only deployments); a warning is printed at startup."""
        want = os.environ.get("WEBHOOK_TOKEN", "")
        if not want:
            return True
        got = self.headers.get("X-Webhook-Token", "")
        if not got:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got = q.get("token", [""])[0]
        return hmac.compare_digest(got, want)

    def do_GET(self):
        self._send(200, b"fuldc-arr-bridge webhook up")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if not self._authorized():
            print(f"[deny] unauthorized webhook from {self.client_address[0]}", flush=True)
            return self._send(401, b"unauthorized")
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
    if not os.environ.get("WEBHOOK_TOKEN"):
        print("! WEBHOOK_TOKEN is not set — anyone who can reach this port can "
              "queue downloads. Set it (and add ?token=… to the Seerr webhook "
              "URL) unless this port is strictly LAN-internal.", flush=True)
    print(f"fuldc-arr-bridge webhook listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
