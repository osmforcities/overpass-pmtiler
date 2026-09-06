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
  are kept — a 100%-missing key is a finding, not noise. No `age` dimension
  is emitted: tiles carry raw `_ts`, and the stats' `editRecencyBands`
  cover age, so age bucketing stays a consumer concern.
- `stats.json` leads with `schemaVersion` (int, currently `1`): additive
  fields don't bump it, removals or meaning changes do. A consumer reads it
  first and refuses what it doesn't understand.
- `recencyBandsDays` (currently `[90, 365, 730]`) states the cutoffs behind
  every recency-derived field: `editRecencyBands` / `mapperRecencyBands`
  bucket by those thresholds (plus an "older" bucket), `recentActivity`
  counts within the first band, and `qualityMetrics` calls elements fresh
  within the second band and stale beyond the last. The output is
  self-describing — no consumer needs out-of-band knowledge of the cutoffs.
- `failed` carries the Overpass (or stage) error text through.
- A periodic sweep removes jobs older than a configurable age regardless
  of ack.

### Interface invariants

- The query must produce Overpass JSON with inline geometry: `[out:json]`
  settings and `out geom` statements. When the request supplies `maxsize`/
  `timeout`, the service injects `[maxsize:]`/`[timeout:]` into the query's
  settings block (overriding any already present) and prepends `[out:json]`
  if the query has no settings block at all; if neither is supplied, the
  query passes through unchanged.
- The response must be Overpass's own pretty-printed JSON — the streaming
  converter splits elements on its line structure (string-aware, so braces
  inside tag values are data). Compact or foreign JSON fails the job at the
  convert stage with a format error instead of baking an empty archive.
- Only `type=multipolygon`/`boundary` relations are assembled; other
  relation types are counted in `relationsSkipped`, never emitted.
- The archive has a single tile layer named `features`.
- Every feature carries `_ts`, a numeric epoch of its last edit (`0` for
  undated relation member nodes) — numeric so tile-side style expressions
  can bucket on it directly.
- `DELETE` aborts a running job: the worker notices on its next progress
  write or state change and the stage unwinds (a mid-bake abort kills the
  tippecanoe subprocess). Immediately re-POSTing a deleted id is safe —
  the recreated record carries a fresh `startedAt`, which is the run's
  generation, and a stale run strands on the mismatch instead of writing
  into the new job's directory.

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

The service is Python 3 stdlib plus `tippecanoe` on PATH — flat modules in
the repo root, one per concern, with `pmtiler.py` as the entry point:

```
pmtiler.py    entry: serve or --selftest; re-exports convert + FIXTURE
config.py     env configuration, shared constants, logging helpers
errors.py     StageError, Aborted (the abort seam)
spool.py      job records on disk, progress, sweep
overpass.py   the fetch stage, query settings, Overpass error extraction
convert.py    the streaming converter, polygon rules, geometry math
bake.py       the tippecanoe stage, archive validation
runner.py     work queue, crash recovery, the serial worker
server.py     the HTTP surface
selftest.py   converter + spool self-checks
```
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
with `PMTILER_OVERPASS`. `python3 check.py --offline` runs the same contract
against an in-process stub Overpass seeded from the selftest fixture — no
network, no Overpass, tippecanoe still required; only the live-data
assertions are replaced with fixture literals. CI (`.github/workflows/check.yml`)
runs the selftest and the offline check on every push and PR; the live mode
stays a manual pre-deploy check.

Current state (v0.1, #2/#3/#4/#5/#6/#7/#12): a closed way becomes a Polygon
only if its tags say so, via the vendored osm-polygon-features ruleset (#12)
— a closed `highway=cycleway` loop stays a LineString unless it carries
`area=yes`, matching osmtogeojson. `type=multipolygon`/`boundary`
relations are assembled into Polygon/MultiPolygon (#6); other relation types
(`route` etc., see #13) and unassemblable ones stay counted in
`relationsSkipped`. An assembled relation's node members (label,
admin_centre) are emitted as bare Points with `_ts: 0` — osmtogeojson parity;
they render in the oldest age band and have no meta. `stats.json` carries
`geometryMix` (`{points, lines, areas, lineKm, areaKm2}`) and, when the job
sets `filterableTags`, `filterDimensions` (#11) — both accumulated in the
convert pass over full-fidelity data, never from tiles. Length and area use
turf's spherical formulas (haversine + Chamberlain–Duquette, R=6371008.8) so
the numbers match `@turf/length` / `@turf/area` bit-for-bit.
The stale-job sweep runs at startup and after each job rather than on a timer;
an idle service never sweeps.

## Deploy

A single Python service plus tippecanoe (apt). Run under systemd; front
with a reverse proxy that serves the output files with Range support.
Example unit and nginx snippet: #8.

## Compatibility targets

The service's outputs are defined against two ecosystem standards, not
against any consumer:

- **Conversion** is osmtogeojson-0.9.2-compatible: same polygon-detection
  ruleset (vendored osm-polygon-features), same relation assembly, same
  member-node emission. Anyone migrating off client-side osmtogeojson gets
  identical feature counts and geometry types.
- **Measures** (`geometryMix.lineKm`/`areaKm2`) are `@turf`-compatible
  spherical geodesy, bit-for-bit.

osmforcities is the first consumer and leaned on this bit-parity while
migrating off client-side conversion; the contracts stand on their own.

`oracle_compare.py` checks the conversion target: it diffs `convert()`
against `osmtogeojson` on a captured fixture — feature counts per element
type, geometry type for every shared feature, ring signatures for relations
(rotation and winding insensitive). osmtogeojson is resolved via npx from a
neighbouring checkout that has it (`PMTILER_APP_DIR`, default `../app`).
Exits nonzero on any mismatch, printing at most 20 per kind:

    PMTILER_APP_DIR=../app python3 oracle_compare.py fixtures/delft-mixed.json

Re-run it whenever the pinned osmtogeojson target bumps (and regenerate
`POLYGON_FEATURES` in `convert.py` from the new package first) — nothing
else detects the two converters drifting apart.

`fixtures/*.json` captures are not in git (up to 11 MB), only the
`.overpassql` that produced them. Regenerate by POSTing the query to an
Overpass instance:

    curl -sk --data-urlencode data@fixtures/delft-mixed.overpassql \
        -o fixtures/delft-mixed.json https://localhost:8080/api/interpreter

`delft-mixed` is the area-decision corpus (#12): Delft `highway`/`barrier`/
`man_made`/`aeroway`, 14,518 ways — where the closed non-area loops live.
The older `delft-parks`/`amsterdam-parks` captures predate query recording
and are all `polygon: all` keys, so they exercise relations but never the
area ruleset.

This design came out of a 2026-09 spike; the probe scripts and render
harness are pruned — findings live in this README and
osmforcities/osmforcities#322, code in git history (`git log -- spike/`).
