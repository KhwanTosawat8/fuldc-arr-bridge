"""Shared helpers for the two HTTP front ends.

Both servers accept requests from the network before they know who is calling,
so the two things they do first — compare a secret and read a body — have to be
safe against hostile input.
"""

from __future__ import annotations

import hmac

# Overseerr payloads are a few KB; a Radarr magnet POST is smaller still. This
# is a sanity ceiling, not a tuning knob.
MAX_BODY_BYTES = 1 * 1024 * 1024


def secure_equal(got: str | None, want: str | None) -> bool:
    """Constant-time comparison that tolerates arbitrary input.

    hmac.compare_digest raises TypeError when given a str containing non-ASCII,
    and every secret we compare arrives from the network (a header, a query
    param, a cookie, a form field). Comparing the UTF-8 bytes keeps the
    constant-time property and makes a hostile token a plain mismatch instead
    of an exception that escapes before the auth decision is made.
    """
    if got is None or want is None:
        return False
    return hmac.compare_digest(got.encode("utf-8", "surrogatepass"),
                               want.encode("utf-8", "surrogatepass"))


def content_length(handler) -> int:
    """Declared body length, or 0 if it isn't a valid one.

    RFC 9110 says Content-Length is ASCII digits. Python's int() is more
    generous than that — int("٣") is 3, and int(" -1 ") is -1 — so parse
    strictly and treat anything else as absent rather than raising, since a
    bare int() here would blow up before any response could be sent.
    """
    raw = (handler.headers.get("Content-Length") or "0").strip()
    if not raw.isascii() or not raw.isdigit():
        return 0
    return int(raw)


def read_body(handler, max_bytes: int = MAX_BODY_BYTES) -> bytes:
    """Read a request body defensively.

    Never allocates more than max_bytes, so an unauthenticated caller cannot
    make us buffer an arbitrary amount on the strength of a header before the
    auth check has even run.
    """
    n = content_length(handler)
    if n <= 0:
        return b""
    return handler.rfile.read(min(n, max_bytes))


def body_too_large(handler, max_bytes: int = MAX_BODY_BYTES) -> bool:
    return content_length(handler) > max_bytes
