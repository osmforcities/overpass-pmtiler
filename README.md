# overpass-pmtiler

Turn an Overpass API query into a PMTiles vector-tile archive, as an async
job. Run it next to your Overpass instance: the multi-GB fetch stays a
localhost read, and only the finished archive (tens of MB) plus a small
stats JSON ever leave the machine.

## How it works

Submit a query as a job; poll it; pull the outputs. Each job flows through
four stages, one job at a time:

```
Overpass ──fetch──> raw.json ──convert──> data.ndjson + stats.json
 (query,           (streamed             (streaming pass, ~13 MB RSS
  localhost)        to disk)              at any input size)
                                             │
                                           bake (tippecanoe)
                                             │
                                          output.pmtiles ──> pulled by you
```

Job state is one JSON file per job in a spool directory — no database, no
framework, no auth (front it with a reverse proxy). Python 3 stdlib plus
`tippecanoe` on PATH; one flat module per concern:

```
pmtiler.py    entry: serve or --selftest
config.py     env configuration, shared constants
errors.py     StageError, Aborted (the abort seam)
spool.py      job records on disk, progress, sweep
overpass.py   the fetch stage, query settings, error extraction
convert.py    the streaming converter, polygon rules, geometry math
bake.py       the tippecanoe stage, archive validation
runner.py     work queue, crash recovery, the serial worker
server.py     the HTTP surface
selftest.py   converter + spool self-checks
```

The modules are the documentation of *how*; each docstring states its
concern, invariants, and known ceilings.

## Use cases

Built for [OSM for Cities](https://github.com/osmforcities/osmforcities)
(per-dataset vector tiles for city-scale OSM data, up to metro scale —
São Paulo's 2.14M buildings bake in ~10 min into a 53 MB archive), but
nothing is specific to it: any application that wants live-data PMTiles
out of an Overpass instance — dataset previews, thematic city maps,
offline extracts — can submit a query and get tiles plus rich dataset
stats back.

## Run it

```
python3 pmtiler.py             # serve on 127.0.0.1:8099
python3 pmtiler.py --selftest  # self-checks, no network
python3 check.py --offline     # full HTTP contract against a stub Overpass
```

Configured entirely by env var: `PMTILER_SPOOL`, `PMTILER_OVERPASS`,
`PMTILER_BIND`/`_PORT`, `PMTILER_INSECURE`, `PMTILER_NICE`,
`PMTILER_MAX_AGE_DAYS` — defaults in `config.py`.

Open `http://127.0.0.1:8099/` for a browser UI: submit a job, watch its
stages, render the archive on a map colored by feature age.

## More

- **[API.md](API.md)** — the job contract, interface invariants, and the
  full `stats.json` field reference. Integrate against this.
- **[examples/](examples/)** — systemd unit + nginx reverse-proxy
  location for deployment; install and upgrade notes in each file.
- **[THREAT-MODEL.md](THREAT-MODEL.md)** — what attacker-editable OSM
  data can and cannot do to this service and its consumers.
- **`check.py`** — reference measurements and the canonical test areas
  (Delft through Tokyo) live in its docstring.
- **`oracle_compare.py`** — the conversion is osmtogeojson-compatible by
  contract; this script is the drift detector.
