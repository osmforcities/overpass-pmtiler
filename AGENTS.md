# overpass-pmtiler

Turns an Overpass query into a PMTiles archive as an async job: fetch -> convert to nd-geojson -> bake with tippecanoe. Python 3 stdlib + tippecanoe on PATH, configured entirely by env var; flat modules with `pmtiler.py` as the entry point. Module map in [README.md](README.md), job contract in [API.md](API.md).

## Commands

```bash
python3 pmtiler.py             # serve (default 127.0.0.1:8099)
python3 pmtiler.py --selftest  # converter, spool + fetch- and bake-failure assertions, loopback only
python3 check.py --offline     # full HTTP contract against a stub Overpass (what CI runs)
python3 check.py               # end-to-end against a real Overpass: one small-city job, then teardown
```

- Needs Overpass for serve and live `check.py`. Locally, point it at a tunnel: `PMTILER_OVERPASS=https://localhost:8080/api/interpreter` and `PMTILER_INSECURE=1` (self-signed cert).
- `http://127.0.0.1:8099/` is a browser view: submit a job, watch per-stage progress, render the archive on a MapLibre map.
- `spool/` holds one dir per job and is gitignored. A large job leaves ~3 GB of transient files; check its size before assuming disk is free.
- Scope is cities, including the largest, not metro or state-level areas, enforced by size alone.

## Rules

- [CODING_STANDARDS.md](CODING_STANDARDS.md): how code here is written. Reviews check against it.
- [CONTEXT.md](CONTEXT.md): domain vocabulary.
- [THREAT-MODEL.md](THREAT-MODEL.md): what hostile OSM data can reach; re-review on its triggers.
- Branch from `main`, PR into `main`.

## Agent skills

### Issue tracker

GitHub issues in `osmforcities/overpass-pmtiler`, blocking edges as native dependencies. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical defaults (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: glossary is the root `CONTEXT.md`, no ADRs yet. See `docs/agents/domain.md`.
