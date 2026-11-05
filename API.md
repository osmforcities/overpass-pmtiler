# API

The integration surface. Everything a consumer must know is on this page;
anything not stated here is an implementation detail free to change.

## Endpoints

```
POST   /jobs      { id, query, maxsize?, timeout?, filterableTags?,
                    keepMeta?, ageBandsDays? }
                  -> 202 { id, state: "queued" }
                  -> 200 { id, state }        idempotent resubmit of a known id
                  -> 400 / 413                validation failures

GET    /status    -> { version, uptimeSeconds, queueDepth, jobStates,
                       spoolBytes, diskFreeBytes }
GET    /jobs      -> [ { id, state, startedAt, progress?, errorKind? } ]
                                                               newest first
GET    /jobs/:id  -> { id, state, startedAt,
                       progress?, error?, errorKind?, stats? }
GET    /jobs/:id/output.pmtiles     the archive (Range supported)
GET    /jobs/:id/stats.json         dataset stats (schema below)
GET    /jobs/:id/data.ndjson        the nd-geojson the archive was baked from
DELETE /jobs/:id  -> 200 { id, state: "deleted" }
                                    ack: outputs are deleted; aborts if running
GET    /          browser viewer (also /viewer.html): submit, watch, render
```

Every `GET` also answers `HEAD`. Errors are JSON `{ error }` with a
human-readable message: `400` validation (including a body that is not a
JSON object or a missing/invalid `Content-Length`), `413` body too large, `404`
unknown route, unknown job, or an output not written yet
(`"output.pmtiles not ready (state baking)"`). Range requests take a
single range (a multi-range request gets the full body); an unsatisfiable
one gets `416` with `Content-Range: bytes */<size>`.

No auth: bind to localhost and only let trusted clients reach it.

`GET /status` is the ops snapshot the dashboard polls (and a monitoring
probe can too): `version` is `pmtiler/` plus the running release from
the `VERSION` file (e.g. `pmtiler/0.1.1`), `jobStates` counts jobs per
state, `queueDepth` is jobs waiting for the serial worker, `spoolBytes` is
the spool's current size on disk, `diskFreeBytes` the free space beside it.

## Request fields

- `id` — caller-supplied, `[A-Za-z0-9._-]{1,128}`, not `.` or `..`.
  POSTing an existing id returns its current state and never re-runs,
  so resubmits are free; the other fields of a resubmit are ignored.
- `query` — Overpass QL, at most 65536 characters (a request body over
  68 KiB is refused with `413`). Must produce JSON with inline
  geometry: `[out:json]` settings and `out geom` output statements.
- `maxsize` (bytes, ≤ 4 GiB) / `timeout` (s, ≤ 3600) — positive JSON integers (not floats, booleans or strings). When supplied, the
  service injects `[maxsize:]`/`[timeout:]` into the query's settings
  block, overriding any already present, and prepends `[out:json]` if the
  query has no settings block; when neither is supplied the query passes
  through unchanged.
- `filterableTags` — optional, up to 32 tag keys of 1 to 255 characters; selects the keys
  reported as `filterDimensions` in the stats.
- `keepMeta` — optional boolean. When true, `data.ndjson` features also
  carry `@user`, `@uid`, `@timestamp` (ISO string), `@version`,
  `@changeset` when the element has them. The archive is unaffected —
  the bake excludes these five keys from tiles, so `output.pmtiles` is
  byte-equivalent to a non-keepMeta job.
- `ageBandsDays` — optional, non-empty, strictly ascending list of up to 8
  integer day cutoffs (1–36500). Adds `ageBands` to the stats: per-FEATURE counts, one
  bucket per cutoff plus a final "older" bucket. Undated features
  (member nodes, timestamp-less elements) count in the final bucket —
  unlike `editRecencyBands`, which counts dated elements only.

## Job lifecycle

States: `queued → fetching → converting → baking → done`, or `failed`.

- `progress` appears only while a stage runs and is cleared on every state
  change: `{stage, bytes}` while fetching (no total — Overpass answers
  chunked), `{stage, features, bytes, total, pct}` while converting,
  `{stage, pct}` while baking. Poll it; nothing is pushed.
- Outputs appear as their stage finishes: `stats.json` and `data.ndjson`
  after converting, `output.pmtiles` after baking. Only `done` guarantees
  all three; an output not yet written answers `404` "not ready".
- `failed` carries the Overpass (or stage) error text in `error`. When a
  fetch-stage failure is recognizably an over-the-ceiling refusal
  (areas-dispatcher refusal or `[maxsize:]` out-of-memory),
  `errorKind: "too_large"` is also set — permanent for that query, do not
  retry. A failure without `errorKind` is unclassified and may be
  transient. An Overpass `429` (rate limit) or `504` (server busy) never
  classifies, whatever its text.
- A query that matches nothing fails after converting with
  `error: "query matched no elements"`; its empty `stats` are kept.
- The archive's max zoom is guessed from feature density. Input whose
  features all sit on one location (a single node, or nodes stacked on one
  spot) has no density to guess from and bakes at a fixed max zoom 14;
  `baking` progress restarts from 0 for that second pass.
- `DELETE` aborts a running job (the stage unwinds at its next progress
  write; a mid-bake abort kills tippecanoe) and immediately re-POSTing the
  same id is safe.
- A restart re-queues any interrupted job FIFO and replays its stages from
  the start; outputs are rewritten, so replays are invisible to consumers.
- A sweep deletes jobs not written to for `PMTILER_MAX_AGE_DAYS` (default
  7) whether or not they were acked — pull outputs promptly. It runs at
  startup and after each job, not on a timer.

## Archive contract

- Single tile layer named `features`.
- Every feature's properties are its raw OSM tags (treat them as text when
  rendering — anyone can author them) plus `@id` (`"way/123"`) and `_ts`,
  a numeric epoch of the element's last edit (`0` for undated relation
  member nodes) — numeric so tile-side style expressions can bucket on it.
- Conversion is osmtogeojson-0.9.2-compatible (see `convert.py` /
  `oracle_compare.py`): tag-driven polygon detection,
  `type=multipolygon|boundary` relation assembly (other relation types are
  counted in `relationsSkipped`, never emitted), member nodes as bare
  Points.

## stats.json

Leads with `schemaVersion` (int, currently `1`): additive fields never
bump it; removals or meaning changes do. Read it first and refuse what you
don't understand.

| field | meaning |
| --- | --- |
| `schemaVersion` | see above |
| `recencyBandsDays` | the cutoffs (currently `[90, 365, 730]`) behind every recency field below — the output is self-describing |
| `features` | emitted feature count = `nodes + ways + relations` |
| `nodes`, `ways`, `relations` | emitted per element type (member nodes count as nodes) |
| `relationsSkipped` | relations not assembled (wrong type or broken geometry) |
| `skipped` | elements the converter could not parse — expect `0` |
| `editorsCount`, `changesetsCount` | distinct users / changesets seen |
| `elementVersionsCount` | sum of element versions |
| `oldestElement`, `mostRecentElement` | ISO timestamps, `null` when nothing is dated |
| `averageElementAge` | days, over dated elements; `null` when nothing is dated |
| `averageElementVersion` | `elementVersionsCount / features` (member nodes count as version 0); `null` when empty |
| `recentActivity` | `{elementsEdited, changesets, editors}` within the first recency band |
| `qualityMetrics` | stale = older than the last band, fresh = within the second; counts + percentages of `features` |
| `editRecencyBands` | element counts per band, plus an "older" bucket (4 entries) |
| `ageBands` | only when `ageBandsDays` was requested: per-feature counts per cutoff plus a final "older" bucket (undated features included there) |
| `mapperRecencyBands` | editors bucketed by their latest edit (4 entries) |
| `tagCounts` | `[{key, count}]`, descending |
| `filterDimensions` | per requested key: `{key, kind:"tag", values:[{value, count}], missing}` — values case-folded with dominant original casing, `;`-split, zero-value keys kept |
| `geometryMix` | `{points, lines, areas, lineKm, areaKm2}` — lengths/areas are `@turf`-compatible spherical geodesy |
| `bbox` | `[w, s, e, n]`, `null` when empty |
| `seconds`, `peakRssMB` | informational: convert-stage wall time; peak RSS of the service process so far, not of this job |

No `age` dimension is emitted: tiles carry raw `_ts` and the stats carry
the bands, so age bucketing stays a consumer concern.
