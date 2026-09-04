# Spike assets (2026-09-04)

Throwaway probe code and the render harness the service design came from
(findings summarized in osmforcities/osmforcities#322 and the top-level
README). Not service code; nothing here ships.

Probe scripts expect an Overpass endpoint at `https://localhost:8080`
(self-signed OK) — point them at your own instance.

- `chunk_probe.py` — chunked bbox `out count` grid probe (from the
  discarded chunking design)
- `monster_probe.py` — worst-case metro building counts via Nominatim +
  Overpass
- `followup_probe.py` — Tokyo/Delhi/SP-state fix-up counts
- `convert_fast.py` — streaming Overpass JSON -> nd-geojson (+accumulator
  stats); 13 MB RSS at 2.23 GB input; `_ts` = numeric epoch (required by
  MapLibre `step` expressions)
- `tile-harness.html` — MapLibre + pmtiles render harness (4-band age
  ramp). Serve the dir with Range support: `npx http-server . -p 8765`
  then open http://localhost:8765/tile-harness.html (expects
  `sp-buildings.pmtiles` beside it).
- `sp-buildings.pmtiles` — not in git (53 MB): Sao Paulo `building=*`,
  2,126,828 features, baked with
  `tippecanoe -zg --drop-densest-as-needed -P -l features`. Regenerate via
  a full-city fetch + `convert_fast.py` + tippecanoe.
