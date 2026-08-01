#!/usr/bin/env python3
"""Seerr -> FulDC++ webhook receiver.

On a Seerr "Media Approved" (or auto-approved) notification for a MOVIE, kicks
off a hybrid grab (immediate download or AutoSearch fallback). Stdlib only.

Env: FULDC_URL, FULDC_USER, FULDC_PASS, DC_ROOT, PORT (default 8080),
     MOVIES_ONLY (default "0"), WEBHOOK_TOKEN (optional shared secret),
     MEDIASERVER (optional post-download library refresh).
"""

from __future__ import annotations

import json
import os
import re
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fuldc_client import FulDCClient
from httputil import body_too_large, read_body, secure_equal
from ranker import Prefs
from core import grab_tv_season, hybrid_grab

APPROVED = {"MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"}
YEAR_RE = re.compile(r"\((\d{4})\)")


def client() -> FulDCClient:
    return FulDCClient(os.environ.get("FULDC_URL", "http://host.docker.internal:5600"),
                       os.environ.get("FULDC_USER", "admin"),
                       os.environ["FULDC_PASS"])


def parse(payload: dict):
    """Pull (notification_type, media_type, title, year) out of a Seerr payload.

    Every field is defensive: the payload template is user-editable in Seerr's
    settings, all values arrive as strings, and `media` is nulled out entirely
    when the notification has no media attached (issue comments, test pings).
    """
    nt = str(payload.get("notification_type") or "")
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    mtype = str(media.get("media_type") or "").lower()
    subject = str(payload.get("subject") or "").strip()
    m = YEAR_RE.search(subject)
    year = int(m.group(1)) if m else None
    title = (YEAR_RE.sub("", subject).strip(" -") if m else subject).strip()
    return nt, mtype, title, year


# A season number outside this range is a parse artefact (a year, an id), not a
# season. Accepting one creates a %[inc] monitor that can never match.
MAX_SEASON = 100


def requested_seasons(payload: dict) -> list[int]:
    """Seerr puts requested seasons in the `extra` array as
    {"name": "Requested Seasons", "value": "1, 2"}. Season 0 is Specials and is
    legitimate; anything above MAX_SEASON is junk."""
    for e in payload.get("extra") or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("name", "")).lower().startswith("requested season"):
            found = {int(x) for x in re.findall(r"\d+", str(e.get("value", "")))}
            good = sorted(n for n in found if 0 <= n <= MAX_SEASON)
            for bad in sorted(found - set(good)):
                print(f"[skip] implausible season number {bad}", flush=True)
            return good
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


def _grab(title, year, *, kind, season=None):
    print(f"[grab] {title!r} ({year}) type={kind}" + (f" S{season:02d}" if season else ""),
          flush=True)
    try:
        c = client()
        res = hybrid_grab(c, title, year, kind=kind, season=season,
                          prefs=_prefs(), dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                          movies_dir=os.environ.get("MOVIES_DIR"),
                          series_dir=os.environ.get("SERIES_DIR"),
                          log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
        _after_download(c, res, kind)
    except Exception:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r}:\n{traceback.format_exc()}", flush=True)


def _grab_season(title, season):
    q = os.environ.get("QUALITY", "").strip() or None
    print(f"[grab] {title!r} series S{season:02d}", flush=True)
    try:
        c = client()
        res = grab_tv_season(c, title, season, prefs=_prefs(),
                             dc_root=os.environ.get("DC_ROOT", "S:\\dc"),
                             movies_dir=os.environ.get("MOVIES_DIR"),
                             series_dir=os.environ.get("SERIES_DIR"),
                             quality=q, log=lambda m: print(m, flush=True))
        print(f"[done] {res}", flush=True)
        _after_download(c, res, "series")
    except Exception:  # noqa: BLE001 - webhook must never crash the server
        print(f"[error] {title!r} S{season}:\n{traceback.format_exc()}", flush=True)


def handle(payload: dict) -> None:
    # This runs on a detached thread after the 200 was already sent, so an
    # escaping exception would vanish into a bare threading traceback with no
    # record of which request died.
    try:
        _handle(payload)
    except Exception:  # noqa: BLE001 - a bad payload must not kill the thread silently
        print(f"[error] unhandled webhook failure for "
              f"subject={payload.get('subject')!r}:\n{traceback.format_exc()}", flush=True)


def _handle(payload: dict) -> None:
    nt, mtype, title, year = parse(payload)
    if nt not in APPROVED:
        print(f"[skip] notification_type={nt}", flush=True)
        return
    if mtype not in ("movie", "tv"):
        # Never guess. Falling through to the movie branch on an unknown type
        # is how a TV show ends up in DC_ROOT\movies\.
        print(f"[skip] unsupported media_type={mtype!r}", flush=True)
        return
    if os.environ.get("MOVIES_ONLY", "0") == "1" and mtype != "movie":
        print(f"[skip] media_type={mtype} (movies only)", flush=True)
        return
    if not title:
        print("[skip] empty title", flush=True)
        return
    if mtype == "tv":
        seasons = requested_seasons(payload)
        if seasons:
            for season in seasons:
                _grab_season(title, season)   # season pack now, else %[inc] monitor
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

    def _authorized(self) -> bool:
        """Optional shared secret. This endpoint queues downloads on your box,
        so if WEBHOOK_TOKEN is set we require it.

        Overseerr can only send a single configured `Authorization` header, so
        that is accepted alongside Jellyseerr's custom X-Webhook-Token and the
        ?token= query form. Unset = open (LAN-only); warned about at startup.
        """
        want = os.environ.get("WEBHOOK_TOKEN", "")
        if not want:
            return True
        auth = self.headers.get("Authorization", "")
        for candidate in (self.headers.get("X-Webhook-Token", ""),
                          auth[7:] if auth[:7].lower() == "bearer " else auth,
                          urllib.parse.parse_qs(
                              urllib.parse.urlparse(self.path).query).get("token", [""])[0]):
            if candidate and secure_equal(candidate, want):
                return True
        return False

    def do_GET(self):
        self._send(200, b"fuldc-arr-bridge webhook up")

    def do_POST(self):
        # Authorize before buffering: an unauthenticated caller must not be
        # able to make us allocate on the strength of a Content-Length header.
        if not self._authorized():
            read_body(self, 0)
            print(f"[deny] unauthorized webhook from {self.client_address[0]}", flush=True)
            return self._send(401, b"unauthorized")
        if body_too_large(self):
            return self._send(413, b"payload too large")
        raw = read_body(self)
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        # The payload template is user-editable in Seerr, so anything can
        # arrive here — including a JSON array or a bare string.
        if not isinstance(payload, dict):
            print(f"[skip] payload is {type(payload).__name__}, not an object", flush=True)
            return self._send(200, b"ignored")
        self._send(200, b"accepted")
        threading.Thread(target=handle, args=(payload,), daemon=True).start()

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PORT", "8080"))
    # Fail at startup, not on the first webhook. Without this the service comes
    # up, answers every health probe 200, and only whispers a KeyError into
    # stdout once a request actually arrives.
    if not os.environ.get("FULDC_PASS"):
        sys.exit("FULDC_PASS is not set — the bridge cannot talk to FulDC++.")
    if not os.environ.get("DC_ROOT"):
        print("! DC_ROOT is not set; falling back to S:\\dc, which is probably "
              "not where your share lives.", flush=True)
    if not os.environ.get("WEBHOOK_TOKEN"):
        print("! WEBHOOK_TOKEN is not set — anyone who can reach this port can "
              "queue downloads. Set it (and add ?token=… to the Seerr webhook "
              "URL) unless this port is strictly LAN-internal.", flush=True)
    print(f"fuldc-arr-bridge webhook listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
