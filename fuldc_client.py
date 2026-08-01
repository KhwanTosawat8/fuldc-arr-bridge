"""Minimal FulDC++ / AirDC++ Web API client (stdlib only).

Verified against FulDC++ 1.08 (api_feature_level 10) over HTTP basic auth.
Covers exactly what the movies MVP needs: search -> results -> download to a
target folder -> track/remove the resulting bundle.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any


class FulDCError(RuntimeError):
    pass


class FulDCClient:
    def __init__(self, base_url: str, user: str, password: str, timeout: int = 25):
        self.base = base_url.rstrip("/") + "/api/v1"
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any | None = None) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Basic {self._auth}",
                     "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode()
            try:
                return e.code, json.loads(body_txt or "{}")
            except json.JSONDecodeError:
                return e.code, {"message": body_txt}
        except (urllib.error.URLError, OSError) as e:
            # connection refused / DNS / timeout — surface as our own error type
            # so callers don't have to catch raw urllib exceptions
            raise FulDCError(f"{method} {path}: cannot reach FulDC++ at "
                             f"{self.base} ({e})") from e

    # --- generic ---------------------------------------------------------
    def system_info(self) -> dict:
        st, data = self._call("GET", "/system/system_info")
        if st != 200:
            raise FulDCError(f"system_info http {st}: {data}")
        return data

    def default_download_dir(self) -> str:
        st, data = self._call("POST", "/settings/get", {"keys": ["download_directory"]})
        return (data or {}).get("download_directory", "") if st == 200 else ""

    # --- search ----------------------------------------------------------
    def search(self, pattern: str, wait: float = 10.0, poll: float = 1.0,
               plateau: float = 3.0) -> tuple[int, list[dict]]:
        """Run a hub search, wait for results to settle, return (instance_id, results).

        Waits up to `wait` seconds, stopping early once result_count has been
        unchanged for `plateau` seconds. Caller is responsible for close().
        """
        st, inst = self._call("POST", "/search")
        if st != 200:
            raise FulDCError(f"create search instance http {st}: {inst}")
        iid = inst["id"]
        st, _ = self._call("POST", f"/search/{iid}/hub_search", {"query": {"pattern": pattern}})
        if st != 200:
            self.close(iid)
            raise FulDCError(f"hub_search http {st}")
        deadline = time.time() + wait
        last_count, stable_since = -1, time.time()
        while time.time() < deadline:
            time.sleep(poll)
            _, cur = self._call("GET", f"/search/{iid}")
            count = (cur or {}).get("result_count", 0)
            if count != last_count:
                last_count, stable_since = count, time.time()
            elif count > 0 and (time.time() - stable_since) >= plateau:
                break
        _, results = self._call("GET", f"/search/{iid}/results/0/200")
        return iid, (results or [])

    def close(self, instance_id: int) -> None:
        self._call("DELETE", f"/search/{instance_id}")

    # --- download / queue ------------------------------------------------
    def download_result(self, instance_id: int, result_id: str,
                        target_directory: str | None = None,
                        name: str | None = None) -> dict:
        """Queue a grouped search result. target_directory is a Windows path
        (trailing backslash added). Returns {'bundle_id':..., 'merged':...}.

        File results return a bundle_info immediately. DIRECTORY results kick off
        a filelist (directory) download first and return `directory_download_ids`,
        so the bundle appears asynchronously — we poll exactly those ids until
        one carries a bundle."""
        body: dict = {}
        if target_directory:
            td = target_directory.replace("/", "\\")
            if not td.endswith("\\"):
                td += "\\"
            body["target_directory"] = td
        st, data = self._call("POST", f"/search/{instance_id}/results/{result_id}/download", body)
        if st != 200:
            raise FulDCError(f"download http {st}: {data}")
        data = data or {}
        bi = data.get("bundle_info") or {}
        if bi.get("id"):
            return {"bundle_id": bi["id"], "merged": bi.get("merged")}
        # Directory result: the API hands back the ids of the directory downloads
        # it started. Poll *those* — scanning the global list would happily pick
        # up a concurrent grab's bundle instead of ours.
        dd_ids = data.get("directory_download_ids") or []
        for _ in range(15):
            for dd_id in dd_ids:
                qb = (self.get_directory_download(dd_id).get("queue_info") or {}).get("bundle") or {}
                if qb.get("id"):
                    return {"bundle_id": qb["id"], "merged": qb.get("merged")}
            if name:
                for b in self.list_bundles():
                    if b.get("name") == name:
                        return {"bundle_id": b["id"], "merged": True}
            time.sleep(1)
        return {"bundle_id": None, "raw": data}

    def get_directory_download(self, dd_id) -> dict:
        st, data = self._call("GET", f"/filelists/directory_downloads/{dd_id}")
        return data or {} if st == 200 else {}

    def list_bundles(self, start: int = 0, count: int = 200) -> list[dict]:
        _, data = self._call("GET", f"/queue/bundles/{start}/{count}")
        return data or []

    def get_bundle(self, bundle_id: int) -> dict | None:
        st, data = self._call("GET", f"/queue/bundles/{bundle_id}")
        return data if st == 200 else None

    def remove_bundle(self, bundle_id: int, remove_finished: bool = True) -> bool:
        st, _ = self._call("POST", f"/queue/bundles/{bundle_id}/remove",
                            {"remove_finished": remove_finished})
        return st in (200, 204)

    # --- autosearch (FulDC++ core module) -------------------------------
    def list_autosearch(self) -> list[dict]:
        _, data = self._call("GET", "/auto_search/items")
        return data or []

    def create_autosearch(self, search_string: str, target_directory: str | None = None,
                          excluded: str = "", file_type: str = "", min_size: int = 0,
                          remove_after_hit: bool = True, action: str = "download") -> dict:
        """Create a persistent AutoSearch item — the client keeps searching and
        auto-downloads (to target_directory) when the release appears. Ideal for
        content nobody is sharing right this moment."""
        body: dict = {
            "search_string": search_string,
            "action": action,
            "matcher_type": "partial",
            "remove_after_hit": remove_after_hit,
        }
        if excluded:
            body["excluded_string"] = excluded
        if file_type:
            body["file_type"] = file_type
        if min_size:
            body["min_size"] = min_size
        if target_directory:
            td = target_directory.replace("/", "\\")
            if not td.endswith("\\"):
                td += "\\"
            body["target"] = td
        st, data = self._call("POST", "/auto_search/items", body)
        if st not in (200, 201):
            raise FulDCError(f"create autosearch http {st}: {data}")
        return data or {}

    def delete_autosearch(self, item_id: int) -> bool:
        st, _ = self._call("DELETE", f"/auto_search/items/{item_id}")
        return st in (200, 204)

    def force_autosearch(self, item_id: int) -> bool:
        st, _ = self._call("POST", f"/auto_search/items/{item_id}/search")
        return st in (200, 204)

    # Bundle status ids, per QueueBundleUtils.cpp: new, queued, recheck,
    # downloaded, download_error, completion_validation_running,
    # completion_validation_error, completed, shared.
    DONE_OK = {"completed", "shared"}           # finished and (re)shared
    DONE_ON_DISK = DONE_OK | {"downloaded"}     # data is on disk; validation may still run
    DONE_BAD = {"download_error", "completion_validation_error"}

    def wait_bundle(self, bundle_id: int, timeout: int = 3600, poll: int = 5,
                    on_status=None) -> dict | None:
        """Poll a bundle until it reaches a terminal status or timeout.
        Calls on_status(status_id, bundle) on each status change. Returns the
        final bundle dict (or None if it vanished)."""
        import time as _t
        deadline = _t.time() + timeout
        last = None
        while _t.time() < deadline:
            b = self.get_bundle(bundle_id)
            if b is None:
                return None
            sid = (b.get("status") or {}).get("id")
            if sid != last:
                if on_status:
                    on_status(sid, b)
                last = sid
            if sid in self.DONE_ON_DISK or sid in self.DONE_BAD:
                return b
            _t.sleep(poll)
        return self.get_bundle(bundle_id)
