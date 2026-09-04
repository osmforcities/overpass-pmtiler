# Spike assets: large datasets / vector tiles (2026-09-04)

Throwaway probe code and the baked test archive from the #322 validation spike.
Findings: SPIKE-CHUNKING.md (repo root) and app/docs/features/large-datasets.md.
Not app code; nothing here ships. Keep out of git or commit deliberately.

- `chunk_probe.py` — chunked bbox `out count` grid probe (needs Overpass tunnel on :8080)
- `monster_probe.py` — worst-case metro building counts via Nominatim + our Overpass
- `followup_probe.py` — Tokyo/Delhi/SP-state fix-up counts
- `convert_fast.py` — streaming Overpass JSON -> nd-geojson (+accumulator stats);
  13 MB RSS at 2.23 GB input; `_ts` = numeric epoch (required by tile style)
- `tile-harness.html` — MapLibre + pmtiles render harness (app's 4-band Viridis
  ramp). Serve dir with Range support: `npx http-server . -p 8765` then open
  http://localhost:8765/tile-harness.html (expects `sp-buildings.pmtiles` beside
  it at that port).
- `sp-buildings.pmtiles` — São Paulo `building=*`, 2,126,828 features, 53 MB,
  baked 2026-09-04 (`tippecanoe -zg --drop-densest-as-needed -P -l features`)
