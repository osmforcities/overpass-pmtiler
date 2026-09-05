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
POST   /jobs      { id, query, maxsize, timeout, filterableTags }
                  -> 202 { id, state }
GET    /jobs/:id  -> { state: queued|fetching|converting|baking|done|failed,
                       startedAt, error?, stats? }
GET    /jobs/:id/output.pmtiles
GET    /jobs/:id/stats.json
GET    /jobs/:id/data.ndjson
DELETE /jobs/:id               (ack -> cleanup)
```

- `id` is supplied by the caller. POST of an existing id returns its
  current state — idempotent resubmits, no ID mapping anywhere.
- `filterableTags` (optional, up to 32 tag keys) selects the keys reported
  as `filterDimensions` in the stats: per-value counts (case-folded, with
  the dominant original casing displayed; `;`-separated values split into
  separate buckets) plus a `missing` count per key. Keys with zero values
  are kept — a 100%-missing key is a finding, not noise. Mirrors the app's
  `computeFilterDimensions(..., { keepEmpty: true })`. No `age` dimension
  is emitted: tiles carry raw `_ts`, and the stats' `editRecencyBands`
  cover age, so age bucketing stays a consumer concern.
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

Canonical test set (any Overpass instance with area support). These are the
one-click buttons in the preview and the list in `viewer.html`:

| case | area | relation | size | measured |
| --- | --- | --- | --- | --- |
| small | Delft | 324269 | 39.6k buildings | 25s, 3.8 MB archive |
| mid | Amsterdam | 271110 | 197k buildings | ~1 min, 197,153 features |
| standard metro | Sao Paulo (municipality) | 298285 | 2.14M buildings | 9m30s, 53.3 MB |
| large state | Sao Paulo (state) | 298204 | 2.71M buildings | not yet run |
| ceiling | Tokyo Metropolis | 1543125 | >3 GiB to even count | expected to fail |

Tokyo stays in the list on purpose: refusing it gracefully is a requirement,
so the ceiling case needs to be as easy to run as the happy ones.

The Sao Paulo row was reproduced end to end on a laptop over an SSH tunnel
(2026-09), landing within 5% of the server-side reference above at every stage
— the pipeline is not sensitive to where it runs.

## Running it

`pmtiler.py` is the whole service — Python 3 stdlib plus `tippecanoe` on PATH.
Everything is configured by env var:

```
PMTILER_SPOOL         spool dir (default ./spool)
PMTILER_OVERPASS      interpreter URL (default http://localhost/api/interpreter)
PMTILER_BIND/_PORT    listen address (default 127.0.0.1:8099)
PMTILER_INSECURE=1    skip TLS verification (dev tunnels with self-signed certs)
PMTILER_NICE          nice level for the bake (default 10)
PMTILER_MAX_AGE_DAYS  sweep jobs older than this (default 7)
```

```
python3 pmtiler.py             # serve
python3 pmtiler.py --selftest  # converter + query-rewrite assertions
python3 check.py               # end-to-end: one small-city job, then teardown
```

Open the service root in a browser to drive it without curl:
`http://127.0.0.1:8099/` lists the spool and has a submit form; `?job=<id>`
follows that job's progress and then renders its archive on a MapLibre map
coloured by the 4-band `_ts` age ramp, with stats, output links and a delete
button. Same-origin, so the PMTiles client's range requests just work.
`GET /jobs` (the list the index reads) is an addition to the contract above.

It is deliberately one dependency-free HTML file with two CDN scripts, not a
bundled app -- there is no build step to run before serving it, and nothing to
rebuild on deploy. Reach for a bundler only if the preview grows past a couple
of views or has to serve its assets locally.

`check.py` runs a real Delft `building=*` job (rel 324269, ~39.5k features)
against a live Overpass and asserts the whole contract. It defaults to the
osmforcities dev tunnel (`https://localhost:8080/api/interpreter`); override
with `PMTILER_OVERPASS`.

Current state (v0.1, #2/#3/#4/#5/#6/#7): `type=multipolygon`/`boundary`
relations are assembled into Polygon/MultiPolygon (#6); other relation types
(`route` etc., see #13) and unassemblable ones stay counted in
`relationsSkipped`. An assembled relation's node members (label,
admin_centre) are emitted as bare Points with `_ts: 0` — osmtogeojson parity;
they render in the oldest age band and have no meta. `stats.json` carries
`geometryMix` (`{points, lines, areas, lineKm, areaKm2}`) and, when the job
sets `filterableTags`, `filterDimensions` (#11) — both accumulated in the
convert pass over full-fidelity data, never from tiles. Length and area use
turf's spherical formulas (haversine + Chamberlain–Duquette, R=6371008.8) so
the numbers match the app's turf-computed stored stats bit-for-bit.
The stale-job sweep runs at startup and after each job rather than on a timer;
an idle service never sweeps.

## Deploy

A single Python service plus tippecanoe (apt). Run under systemd; front
with a reverse proxy that serves the output files with Range support.
Example unit and nginx snippet: #8.

## Spike code

`spike/` holds the probe scripts and render harness this design came from
(2026-09). The baked `sp-buildings.pmtiles` (53 MB) is deliberately not in
git — regenerate with `spike/convert_fast.py` + tippecanoe.
