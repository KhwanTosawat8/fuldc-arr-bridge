# FulDC++ / AirDC++ ↔ *arr bridge — design spec

Bring the Radarr/Sonarr/Seerr automation experience to Direct Connect, using
FulDC++ (AirDC++-based) as the transport. **Nothing bypasses DC encryption and
nothing calls out to third-party services** — the bridge is self-hosted and only
talks to the client's local Web API and to your own *arr/Seerr instances.

Verified working against FulDC++ 1.08 (api_feature_level 10) at `mgmt:5600`
(HTTP basic auth): create search instance → `hub_search` → grouped results →
`download-result` to a target folder, plus queue/bundle events for completion.

---

## Why two components (the *arr model)

Radarr/Sonarr never talk to a "downloader" directly. They need:

1. an **indexer** — searched on demand *and* polled via RSS for new releases;
2. a **download client** — handed a chosen release, then polled for progress and
   final path, keyed by a **category**.

So the bridge must present **both** faces. Both are thin translation layers over
the AirDC++ Web API + the hub's release RSS. (This matches LUCiDREAM's read:
"rss ska kunna gå även om man confar sonarr/radarr som en nedladdningsklient".)

```
                    ┌────────────────────── bridge (self-hosted, LAN only) ──────────────────────┐
 Radarr/Sonarr ──►  │  A) Torznab indexer  ── search/RSS ──►  AirDC++ Web API  (search instances) │
 (or Prowlarr)      │                                                                             │
 Radarr/Sonarr ──►  │  B) qBittorrent-API shim ── add/track ─► AirDC++ Web API (download-result,  │
                    │                                            queue/bundle events)             │
                    └─────────────────────────────────────────────────────────────────────────────┘
                                 │ writes RAR release folder to dc/movies|dc/series (SMB→TrueNAS)
                                 ▼
                        rar2fs on Plex → Plex scan → (Seerr flips Available)

 Lightweight alt: Seerr "Media Approved" webhook ──► bridge ──► search + download-result (no *arr)
```

Two deployment flavors share the same core:
- **Full *arr** = A + B (quality profiles, renaming, native Seerr integration).
- **Seerr-direct** = Seerr webhook → search → `download-result` (simplest; manual
  approve stays the gate; no Radarr/Sonarr). Ship this first.

---

## AirDC++ search-result schema (live, for reference)

Grouped directory result (movie RAR folder):

| field | example | use |
|---|---|---|
| `id` | `LJGZ3O5…` | download id **within the live search instance** (ephemeral) |
| `path` | `/-x264-Kids/…/Julkalendern.2021.En.hederlig.Jul.Med.Knyckertz/1080p/` | derive **release title** (parent of quality subdir) + category |
| `size` | `8498318738` | exact-size re-match key; Torznab `size` |
| `tth` | `""` (dirs) / TTH (files) | exact re-match for files |
| `type.id` | `directory`\|`file` | grouping |
| `slots.free` / `slots.total` | `34/35` | availability |
| `users.count` | `2` | Torznab `seeders` |
| `relevance` | `1.02` | ranking |
| `dupe` | `null` | detect already-in-share/queue → skip |
| `users.user.cid` / `hub_url(s)` | … | hub hint; IP `country` for language filter |

**Key consequence:** directories have **no TTH**, so movie releases re-match by
`(path, exact size)`. The **release name is in `path`**, not `name`.

---

## Component A — Torznab indexer

Add in Prowlarr/Radarr/Sonarr as **Generic Torznab**, URL `http://bridge:9117/`,
plus an API key. Implement the Newznab/Torznab XML API:

- `?t=caps` → capabilities XML: categories (2000 Movies, 5000 TV), supported
  search params (`q`, `imdbid`, `tmdbid`, `tvdbid`, `season`, `ep`).
- `?t=search|movie|tvsearch&q=…&cat=…` → translate to a `hub_search`, collect
  ~8–10 s, return `<item>`s.
- **RSS sync** (`t=…` with empty `q`) → serve recent releases from the **hub
  release RSS** (if available) mapped to the same `<item>` shape; else empty.

### DC result → Torznab `<item>` mapping

| Torznab / RSS field | from AirDC++ result |
|---|---|
| `title` | **release name derived from `path`** (deepest segment parsing as a release: has year/quality tokens); fallback to folder name |
| `size` | `size` (bytes) |
| `guid` / `enclosure@url` | `magnet:?xt=urn:btih:<synthetic>&dn=<title>&xl=<size>` (see round-trip) |
| `pubDate` | `time` |
| `category` | derive from `path` prefix (`-x264-…`, quality) + query cat |
| `torznab:attr seeders` | `users.count` |
| `torznab:attr peers` | `users.count` |
| `torznab:attr downloadvolumefactor` | `0` |
| `torznab:attr uploadvolumefactor` | `1` |

Prefer scene-named releases in ranking — **Radarr rejects titles it can't
parse**, so titles that don't yield title+year+quality are down-weighted or
dropped. This is the single hardest part (raw folder names, no metadata).

---

## Component B — qBittorrent WebUI API shim

Add in Radarr/Sonarr as a **qBittorrent** download client → `http://bridge:8080`.
Emulate the v2 API (minimum surface):

| endpoint | behavior |
|---|---|
| `POST /api/v2/auth/login` | accept configured creds → return SID cookie |
| `GET /api/v2/app/version`, `/webapiVersion` | return plausible versions (pass compat check) |
| `GET /api/v2/app/preferences` | return `save_path` etc. |
| `GET /api/v2/torrents/info?category=radarr` | list tracked downloads from AirDC++ **bundles** |
| `POST /api/v2/torrents/add` | receive magnet (`urls`) + `category` → decode → re-acquire → `download-result` to category folder |
| `POST /api/v2/torrents/delete` | untrack (+ optionally remove bundle) |
| `GET /api/v2/torrents/properties`, `/files` | fill from bundle |

### Bundle → qBit torrent-state mapping

| qBit `state` | condition (AirDC++ bundle) |
|---|---|
| `downloading` | bundle downloading, sources present |
| `stalledDL` | queued, **no sources right now** (retry loop, see below) |
| `uploading` / `pausedUP` | bundle **finished** → signals Radarr to import |
| `error` | bundle failed / validation error |

Report per item: `hash`, `name`, `progress`, `state`, `size`, `amount_left`,
`dlspeed`, `save_path`, **`content_path`** (final folder → Radarr imports here).

**Category → folder:** `radarr`→`dc/movies`, `sonarr`→`dc/series` (configurable).
Mirrors how Radarr/Sonarr locate completed downloads.

---

## The download round-trip (the crux)

DC search results are **ephemeral** (bound to a live search instance) and dirs
have **no TTH**, so the download URL must let the bridge **re-acquire** the item
minutes later, at grab time.

1. **Indexer** emits a synthetic magnet:
   `btih = sha1( tth  OR  path + "|" + size )[:40 hex]`.
   Bridge stores `synthetic_hash → { pattern, match:{tth | (path,size)}, type, title, size, target_category }` (SQLite).
2. **Radarr grabs** → sends the magnet to the qBit shim `torrents/add`.
3. **Shim re-acquires**: new search instance → `hub_search(pattern)` (or an exact
   **TTH search** when a TTH exists) → find grouped result where
   `tth == …` (files) or `path == … && size == …` (dirs) → `POST
   /search/{inst}/results/{grouped_id}/download` with target = category folder.
4. Map returned **bundle id → synthetic_hash**; track via queue/bundle events;
   report status back through `torrents/info` until `content_path` is final.

TTH searches are exact + indexed (cheap); name+size is the directory fallback.

---

## Availability / retry

If re-acquire finds no sources (nobody sharing right now), keep the "torrent" in
`stalledDL` and **re-search on a backoff** (wanted-list loop) until sources
appear — a natural fit for DC, better than a one-shot torrent grab. After a
configurable deadline, report `error` so Radarr can try the next release.

---

## RAR + rar2fs + Plex import

Hubs force RAR shares → bundles are RAR sets. Options for the import hop:
- Report the **rar2fs virtual path** as `content_path` so Radarr/Plex see a real
  `.mkv` (copy-import; hardlinks won't cross FUSE).
- Or report the raw folder and let rar2fs/Plex handle presentation (no rename).
No extraction step needed — aligns with the existing rar2fs setup.

---

## Ranking / matching heuristics (bridge-side)

title+year match (from `path`) · prefer quality keywords (1080p, x265, language
e.g. SweSub via source IP country) · reject sample/CAM/TS · size window · prefer
higher `users.count`/`slots.free` · honor `dupe` to skip already-have. Iterative.

---

## Where webhooks/scripts fit (LUCiDREAM)

- **Trigger in:** Seerr "Media Approved" webhook (Seerr-direct flavor) or Radarr
  RSS/search (full flavor).
- **Completion out:** ideal = FulDC++ **bundle-finished → run script/webhook** so
  the bridge doesn't have to hold a WebSocket; the completion webhook then fires
  a targeted Plex scan. If not available, poll the queue/bundle WS event.

---

## Open questions for the client / hub devs

1. Official/recommended pattern for programmatic auto-download beyond
   `airdcpp-auto-downloader.js`?
2. Is **AutoSearch** reachable via the API (for the retry-when-unavailable loop),
   or GUI/config only?
3. Is the **RSS reader** readable/manageable via the API, and is there a
   structured **per-hub release RSS** the indexer can wrap? (Format/fields?)
4. Interest in a native **completion webhook/script hook** on bundle finish?
5. Any client-side **rate-limit / search-etiquette** guidance for an automated
   client polling for wanted items, so it stays hub-friendly?
6. Confirm `download-result` body params for **directory** results (target dir +
   priority) and whether a filelist fetch is implied.

---

## Build order

1. **Seerr-direct MVP (movies):** webhook → search → rank → `download-result` to
   `dc/movies` → completion → Plex scan. (No *arr; manual approve gate.)
2. Harden ranking + the availability retry loop on real requests.
3. **Component A (Torznab)** wrapping hub RSS + on-demand search.
4. **Component B (qBittorrent shim)** → full Radarr/Sonarr.
5. TV (season/episode logic).
