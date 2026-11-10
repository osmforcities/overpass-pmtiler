# Glossary

The service's own vocabulary, one line per term.

- **Job**: one submitted Overpass query, identified by a caller-supplied id, run through the stages by the serial worker. _Avoid:_ task, request, build.
- **Spool**: the directory holding one subdirectory per job (its `job.json` record plus stage files and outputs); the only state the service has. _Avoid:_ cache, database, workdir.
- **Stage**: one step of a job: fetch, convert or bake, run in that order. _Avoid:_ step, phase.
- **Fetch**: the stage that streams the Overpass response to `raw.json` on disk. _Avoid:_ download, query.
- **Convert**: the stage that streams `raw.json` into `data.ndjson` (osmtogeojson-compatible nd-geojson) plus stats. _Avoid:_ parse, transform.
- **Bake**: the stage that runs tippecanoe over `data.ndjson` (plus tile-join for a split bake) to produce the archive. _Avoid:_ build, tile, render.
- **Archive**: the `output.pmtiles` file a finished job produces. _Avoid:_ tileset, tiles, pmtiles file.
- **Stats**: the `stats.json` dataset summary written by convert (counts, measures, tag coverage, recency). _Avoid:_ metadata, report, summary.
