# overpass-pmtiler

Async extraction + PMTiles bake service co-located with the OSM for Cities
Overpass instance.

## Why

The app serves per-dataset vector tiles (PMTiles) instead of inline GeoJSON
(osmforcities/osmforcities#322). Baking happens next to Overpass for data
locality: a metro-scale fetch is 2.23 GB on the wire but a localhost read
here; only the ~53 MB archive plus a stats JSON ship to the app box.

Division of labor:

- **App**: size detection (count probe), status UX, DB rows, tile serving.
  Pulls finished outputs and serves them from its own nginx/Cloudflare.
- **This service**: fetch (localhost Overpass), stream-convert to
  nd-geojson, stats accumulation, tippecanoe bake.

Decisions (2026-09-05):

- App box pulls outputs. This box stays private (IP-allowlisted nginx);
  serving availability is decoupled from this box being up.
- ALL datasets bake here — single bake path, no tippecanoe on the app box.
- This service is the single Overpass data client: job input is the
  Overpass query itself, fetched via localhost.

## Job contract

```
POST   /pmtiler/jobs      { id, query, maxsize, timeout } -> 202 { id, state }
GET    /pmtiler/jobs/:id  -> { state: queued|fetching|converting|baking|done|failed,
                               startedAt, error?, stats? }
GET    /pmtiler/jobs/:id/output.pmtiles
GET    /pmtiler/jobs/:id/stats.json
GET    /pmtiler/jobs/:id/data.ndjson   (small datasets: app pulls to fill JSONB;
                                        metro export: app proxies on demand)
DELETE /pmtiler/jobs/:id               (ack -> cleanup)
```

- `id` is supplied by the app (`{datasetId}-{snapshotVersion}`). POST of an
  existing id returns its current state — idempotent resubmits, no ID
  mapping anywhere.
- `failed` carries the Overpass error text through.
- Cron sweep removes jobs older than N days regardless of ack.

## Internals

Deliberately boring: one Python service. Jobs run **one at a time** off a
queue — serial execution doubles as contention control (at most one bake
competes with live Overpass queries). Job state = one JSON file per job in
a spool dir; no database. Fetch = streamed localhost `POST /api/interpreter`
to disk. Bake = apt tippecanoe under `nice`:
`tippecanoe -zg --drop-densest-as-needed -P -l features`, with numeric
epoch `_ts` per feature (string `_ts` silently breaks the client's age
ramp).

## Measured facts (2026-09-04 spike; box: 12 vCPU / 64 GB)

Sao Paulo buildings (rel 298285, 2.14M elements), end to end ~10 min:

| stage | result |
| --- | --- |
| fetch (`out geom meta`) | 2.23 GB in 312s at 768 MiB maxsize |
| stream-convert to nd-geojson | 64s, 13 MB peak RSS |
| tippecanoe bake | 194s, 1.2 GB peak RSS |
| output | 53 MB PMTiles (42:1 vs wire) |

- Overpass `maxsize` is driven by area evaluation, not output size.
  Usable per-query ceiling on this instance: between 1 and 3 GiB
  (>= 4 GiB fails with an areas-dispatcher protocol error).
- Streaming conversion must split the `elements` array on element
  boundaries and `JSON.parse` per element — token-level streaming is
  ~40x slower; osmtogeojson is superlinear and unusable at this scale.
- Disk: ~3 GB transient per metro job (raw JSON + nd-geojson) + ~53 MB
  durable until ack.

Canonical stress datasets:

| case | area | relation | size |
| --- | --- | --- | --- |
| standard metro | Sao Paulo (municipality) | 298285 | 2.14M buildings |
| biggest servable | Sao Paulo (state) | 298204 | 2.71M buildings |
| must fail gracefully | Tokyo Metropolis | 1543125 | >3 GiB to even count |

## Spike code

`spike/` holds the probe scripts and render harness this design came from
(2026-09-04). The baked `sp-buildings.pmtiles` (53 MB) is deliberately not
in git — regenerate with `convert_fast.py` + tippecanoe. See
`spike/README.md`.

## Deploy

Ansible role in the infra repo installs and runs the service (systemd unit
`pmtiler.service`, nginx location `/pmtiler/` on the existing allowlisted
vhost). This repo ships the service code only.
