# overpass-pmtiler

A small HTTP service that runs an Overpass query and turns the result into a PMTiles archive. It was built for [OSM for Cities](https://github.com/osmforcities/osmforcities).

## Usage

1. Send an Overpass query to `POST /jobs`. The service queues a job for it.
2. Check `GET /jobs/:id` until the job is `done`.
3. Download the archive from `/jobs/:id/output.pmtiles`.

Full API reference: [API.md](API.md).

## Requirements

You need Python 3, [tippecanoe](https://github.com/felt/tippecanoe) (with its `tile-join`) on your PATH, and access to an Overpass API instance. There are no Python dependencies to install.

## Running

```
python3 pmtiler.py
```

Open `http://127.0.0.1:8099/` to submit a job and preview the archive on a map.

The service has no authentication. Keep it bound to localhost and only let trusted clients reach it.

### Settings

Environment variables, all optional. Read in `config.py`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PMTILER_OVERPASS` | `http://localhost/api/interpreter` | Overpass interpreter URL |
| `PMTILER_USER_AGENT` | `overpass-pmtiler/<version>` | User-Agent sent to Overpass. Some instances reject requests without a client name |
| `PMTILER_INSECURE` | unset | `1` skips TLS verification, for a self-signed cert (e.g. a tunnel) |
| `PMTILER_BIND` | `127.0.0.1` | Listen address |
| `PMTILER_PORT` | `8099` | Listen port |
| `PMTILER_SPOOL` | `spool` | Spool directory, relative to the working directory. A large job can leave about 3 GB |
| `PMTILER_MAX_AGE_DAYS` | `7` | Jobs not written to for this long are swept from the spool |
| `PMTILER_NICE` | `10` | `nice` level for tippecanoe and tile-join |

Example, Overpass behind a local tunnel:

```
PMTILER_OVERPASS=https://localhost:8080/api/interpreter PMTILER_INSECURE=1 python3 pmtiler.py
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
