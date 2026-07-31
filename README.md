# fuldc-arr-bridge

**Request movies & TV in Seerr / Jellyseerr / Overseerr and have them download
automatically over Direct Connect using [FulDC++](https://fuldcpp.net).**

When a request is approved, the bridge searches your DC hubs via the FulDC++
Web API, picks the best release, and downloads it straight into your share — or,
if nobody is sharing it right now, it creates a **FulDC++ AutoSearch** item so
the client grabs it automatically as soon as it appears.

It's the "Radarr/Sonarr experience" for Direct Connect — which normally isn't
possible, because Radarr/Sonarr only drive Usenet and BitTorrent clients.

> **Private and self-contained.** The bridge only talks to your **local** FulDC++
> Web API. It never touches your files, never weakens DC's encryption, and never
> calls out to any third-party service. Everything stays on your machine.

## How it works

```
 someone requests a movie/show in Seerr
        │  (approved / auto-approved)
        ▼  webhook
 fuldc-arr-bridge ──► FulDC++ Web API ──► searches your hubs, ranks releases
        │                                  ├─ available now → download best release
        │                                  └─ not shared yet → AutoSearch (auto-grab later)
        ▼
 lands in  S:\dc\movies\...   or   S:\dc\series\<Show>\S01\...
        │  (FulDC++ re-shares it on the hub)
        ▼
 your media server (Plex/Jellyfin/Emby) scans → request shows as Available
```

The bridge is **stateless and API-only** — it just tells FulDC++ what to search
for and where to save it. No volume mounts, no database.

## Requirements

- **[FulDC++](https://fuldcpp.net)** (Windows) with the **Web UI enabled** and a
  **web user** that has search/download/queue permissions.
- A request frontend: **[Seerr](https://seerr.dev)** / **Jellyseerr** /
  **Overseerr**, connected to your media server (Plex, Jellyfin, or Emby).
- **Docker** — Docker Desktop on Windows works great; run it on the same PC as
  FulDC++.

## Install (Docker Compose — recommended)

```bash
# 1. In FulDC++: enable the Web UI (Settings) and create a web user.
# 2. Grab the files
git clone https://github.com/Pete1979/fuldc-arr-bridge
cd fuldc-arr-bridge
cp .env.example .env        # then edit .env (FULDC_PASS at minimum)
# 3. Start it
docker compose up -d
```

`docker compose logs -f` will show requests as they come in.

If FulDC++ runs on the **same Windows PC**, the default `FULDC_URL` of
`http://host.docker.internal:5600` just works. Otherwise set it to the FulDC++
host's LAN IP, e.g. `http://192.168.0.22:5600`.

## Configure the Seerr / Jellyseerr / Overseerr webhook

**Settings → Notifications → Webhook**:

| field | value |
|---|---|
| Webhook URL | `http://<docker-host>:8080/` (if Seerr runs in Docker on the same host, `http://host.docker.internal:8080/`) |
| JSON Payload | leave the **default** |
| Notification Types | enable **Request Approved** *and* **Request Automatically Approved** |

> ⚠️ **Enable both approval types.** The server **owner's own requests are
> auto-approved**, which fires `MEDIA_AUTO_APPROVED` — not `MEDIA_APPROVED`. If
> you only tick "Request Approved", your own requests won't trigger anything.
> Leave **Request Pending Approval** off (you don't want to grab before approving).

Then request something → approve it → watch `docker compose logs -f`.

## Configuration

| env | default | notes |
|---|---|---|
| `FULDC_URL` | `http://host.docker.internal:5600` | FulDC++ Web API address |
| `FULDC_USER` | `admin` | FulDC++ web user |
| `FULDC_PASS` | — | **required** |
| `DC_ROOT` | *(required)* | **your** DC share root on the FulDC++ host, a Windows path (e.g. `S:\dc`, `D:\Media`). `movies→DC_ROOT\movies\`, `series→DC_ROOT\series\<Show>\S<NN>\` |
| `MOVIES_DIR` / `SERIES_DIR` | *(from DC_ROOT)* | optional full-path overrides for non-standard layouts |
| `MOVIES_ONLY` | `0` | `1` = only movies, `0` = movies + TV |
| `MEDIASERVER` | `none` | optional post-download refresh: `plex` \| `jellyfin` \| `webhook` \| `none` |
| `PORT` | `8080` | webhook listen port |

Optional media-server refresh (most servers scan periodically anyway):
`plex` → `PLEX_URL` + `PLEX_TOKEN`; `jellyfin` → `JELLYFIN_URL` + `JELLYFIN_TOKEN`;
`webhook` → `NOTIFY_WEBHOOK`.

## What it does with a request

- **Movies** → best-ranked release into `DC_ROOT\movies\`.
- **TV** → per requested season into `DC_ROOT\series\<Show>\S<NN>\`, preferring
  full **season packs** over single episodes.
- **Available now** → downloads immediately. **Not shared yet** → creates an
  AutoSearch item so FulDC++ grabs it when it appears.
- Ranking uses title/year/quality/language and skips CAM/TS/sample and content
  you already have. DC has no metadata like torrent indexers, so matching is
  heuristic — tune with the CLI (below) before trusting it fully.

## CLI (for testing / manual use)

The image also ships the `bridge.py` CLI:

```bash
docker compose run --rm fuldc-arr-bridge python bridge.py search "Dune" --year 2021
docker compose run --rm fuldc-arr-bridge python bridge.py grab "Dune" --year 2021 --grab
docker compose run --rm fuldc-arr-bridge python bridge.py grab "Severance" --kind series --grab
```

`search` is read-only; `grab` is a dry run unless you add `--grab`.

## Advanced: Kubernetes

Manifests for a Talos/k8s deployment (Deployment + Service + ExternalSecret) are
in [`k8s/`](k8s/). Point `FULDC_URL` at your FulDC++ host and supply `FULDC_PASS`
via your secret manager.

## Notes & limitations

- Release **matching is heuristic** — DC filenames carry no structured metadata.
- Requires **FulDC++** specifically (it exposes the `auto_search` / `rss` core API
  modules that the upstream AirDC++ webclient does not).
- Roadmap: full Radarr/Sonarr integration via a Torznab indexer + a
  qBittorrent-compatible download-client shim (see [DESIGN.md](DESIGN.md)).

## Credits

Built on the [AirDC++ Web API](https://airdcpp.docs.apiary.io/). Thanks to the
**FulDC++** developers (Sulan & Lansh) and **AirDC-NG** for confirming the
`auto_search` / RSS API surface.

## License

[MIT](LICENSE)
