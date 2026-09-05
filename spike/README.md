# Spike assets (2026-09-04)

The probe scripts and render harness the service design came from have been
pruned (2026-09-05) — findings are summarized in osmforcities/osmforcities#322
and the top-level README; `git log -- spike/` keeps the code. What remains is
the one piece that is still live tooling:

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

Probes expect an Overpass endpoint at `https://localhost:8080` (self-signed
OK) — history has them, if you ever need them back.
