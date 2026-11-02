# overpass-pmtiler

A small HTTP service that runs an Overpass query and turns the result into a PMTiles archive. It was built for [OSM for Cities](https://github.com/osmforcities/osmforcities).

## Usage

1. Send an Overpass query to `POST /jobs`. The service queues a job for it.
2. Check `GET /jobs/:id` until the job is `done`.
3. Download the archive from `/jobs/:id/output.pmtiles`.

Full API reference: [API.md](API.md).

## Requirements

You need Python 3, [tippecanoe](https://github.com/felt/tippecanoe) on your PATH, and access to an Overpass API instance. There are no Python dependencies to install.

## Running

```
python3 pmtiler.py
```

The service listens on `127.0.0.1:8099` and expects Overpass at `http://localhost/api/interpreter`. Open the service address in a browser to submit a job and preview the result on a map. Settings are environment variables, listed with their defaults in `config.py`.

The service has no authentication. Keep it bound to localhost and only let trusted clients reach it.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
