# Contributing

Branch from `main` and open your pull request against `main`.

Before pushing, run the same checks CI runs. Neither needs an Overpass instance, but the second needs tippecanoe.

```
python3 pmtiler.py --selftest
python3 check.py --offline
```

Code follows [CODING_STANDARDS.md](CODING_STANDARDS.md), and [CONTEXT.md](CONTEXT.md) defines the terms used across the code and docs. If a change touches the HTTP API or `stats.json`, update [API.md](API.md) and add a check for it in `check.py` in the same pull request. Changes that affect how OSM data is parsed or baked should be read against [THREAT-MODEL.md](THREAT-MODEL.md).
