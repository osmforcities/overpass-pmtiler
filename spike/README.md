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
- `relcount.py` — relation share by element count, per template shape,
  using the consuming app's query form (node+way+relation per OR group).
  Sizes what #6 is worth: 0.13%-2.86% of features in the sampled cities,
  0% for point and way templates.
- `relarea.py` — the same question by area, since relations are the large
  features (up to 7.4% of area). Sums a shoelace per member way, which is
  only sound where outer members are already closed rings, so it reports a
  fragmented count as its own reliability check — read the areas as a
  floor. Assembling those rings properly is #6 itself.
- `oracle_compare.py` — diffs `pmtiler.convert()` against `osmtogeojson`
  (resolved via npx from the app checkout; point `PMTILER_APP_DIR` at it when
  the app is not a sibling of this repo). Compares feature counts per element
  type, geometry type for every shared feature, and ring signatures for
  relations. Exits nonzero on any mismatch, printing at most 20 per kind:
  `PMTILER_APP_DIR=../app python3 spike/oracle_compare.py fixtures/delft-mixed.json`
- `fixtures/*.json` — not in git (up to 11 MB), only the `.overpassql` that
  produced them. Regenerate by POSTing the query to an Overpass instance:
  `curl -sk --data-urlencode data@fixtures/delft-mixed.overpassql -o fixtures/delft-mixed.json https://localhost:8080/api/interpreter`
  `delft-mixed` is the area-decision corpus (#12): Delft `highway`/`barrier`/
  `man_made`/`aeroway`, 14,518 ways, which is where the closed non-area loops
  live. The older `delft-parks`/`amsterdam-parks` captures predate this note
  and their queries were not recorded; they are all `polygon: all` keys, so
  they exercise relations but never the area ruleset.
- `tile-harness.html` — MapLibre + pmtiles render harness (4-band age
  ramp). Serve the dir with Range support: `npx http-server . -p 8765`
  then open http://localhost:8765/tile-harness.html (expects
  `sp-buildings.pmtiles` beside it).
- `sp-buildings.pmtiles` — not in git (53 MB): Sao Paulo `building=*`,
  2,126,828 features, baked with
  `tippecanoe -zg --drop-densest-as-needed -P -l features`. Regenerate via
  a full-city fetch + `convert_fast.py` + tippecanoe.
