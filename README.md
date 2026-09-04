# overpass-pmtiler

Turn an Overpass API query into a PMTiles archive. An async extraction +
bake service designed to run next to an Overpass instance.

## What it does

Submit an Overpass QL query as a job. The service:

1. fetches the result from Overpass, streamed straight to disk (metro-scale
   results are multiple GB — nothing is buffered in memory),
2. converts it to newline-delimited GeoJSON in a single streaming pass,
   accumulating dataset stats along the way,
3. bakes a PMTiles vector-tile archive with tippecanoe,
4. serves the outputs for the caller to pull.

Designed for data locality: run it on (or next to) the Overpass server so a
multi-GB fetch is a localhost read; only the archive (tens of MB) plus a
small stats JSON leave the machine.

Born from OSM for Cities (osmforcities/osmforcities#322) — per-dataset
vector tiles for city-scale OSM datasets — but nothing here is specific to
it: any application that wants live-data PMTiles out of an Overpass
instance can use it.

## Job contract

```
POST   /jobs      { id, query, maxsize, timeout } -> 202 { id, state }
GET    /jobs/:id  -> { state: queued|fetching|converting|baking|done|failed,
                       startedAt, error?, stats? }
GET    /jobs/:id/output.pmtiles
GET    /jobs/:id/stats.json
GET    /jobs/:id/data.ndjson
DELETE /jobs/:id               (ack -> cleanup)
```

- `id` is supplied by the caller. POST of an existing id returns its
  current state — idempotent resubmits, no ID mapping anywhere.
- `failed` carries the Overpass (or stage) error text through.
- A periodic sweep removes jobs older than a configurable age regardless
  of ack.

## Design principles

- **One job at a time.** The serial queue doubles as contention control:
  a bake never competes with more than itself for the CPU that also serves
  interactive Overpass queries.
- **No database.** Job state is one JSON file per job in a spool
  directory. Outputs are immutable per job id.
- **Streaming everywhere.** Element-boundary splitting + per-element parse
  keeps conversion at ~13 MB RSS regardless of input size. Token-level
  streaming parsers are ~40x slower; whole-collection converters
  (osmtogeojson) are superlinear — both unusable at metro scale.
- **Private by design.** The service has no auth. Bind it to localhost and
  expose it only through a reverse proxy restricted to trusted clients.

## Reference measurements (12 vCPU / 64 GB host, 2026-09)

Sao Paulo `building=*` (relation 298285, 2.14M elements), end to end
~10 min:

| stage | result |
| --- | --- |
| fetch (`out geom meta`) | 2.23 GB in 312s at 768 MiB maxsize |
| stream-convert to nd-geojson | 64s, 13 MB peak RSS |
| tippecanoe bake | 194s, 1.2 GB peak RSS |
| output | 53 MB PMTiles (42:1 vs wire) |

- Overpass `[maxsize:]` is driven by area evaluation, not output size; the
  default 512 MiB fails metro-scale queries that succeed fine at 768 MiB.
- Tiles carry a numeric epoch `_ts` per feature (bake flag set); a string
  `_ts` silently breaks MapLibre `step` expressions downstream.
- Plan ~3 GB transient disk per metro-scale job (raw JSON + nd-geojson) on
  top of the archive.

Useful stress datasets (any Overpass instance with area support):

| case | area | relation | size |
| --- | --- | --- | --- |
| standard metro | Sao Paulo (municipality) | 298285 | 2.14M buildings |
| large state | Sao Paulo (state) | 298204 | 2.71M buildings |
| very large | Tokyo Metropolis | 1543125 | >3 GiB to even count |

## Deploy

A single Python service plus tippecanoe (apt). Run under systemd; front
with a reverse proxy that serves the output files with Range support.
Example unit and nginx snippet: #8.

## Spike code

`spike/` holds the probe scripts and render harness this design came from
(2026-09). The baked `sp-buildings.pmtiles` (53 MB) is deliberately not in
git — regenerate with `spike/convert_fast.py` + tippecanoe.
