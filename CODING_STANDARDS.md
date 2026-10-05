# Coding Standards

How code in this repo should be written. Reviewers (human and agent) check changes against this file.

## Dependencies

- Python 3 standard library only. No `pip install`, no `requirements.txt`.
- `tippecanoe` on PATH is the only external tool. It is invoked with a fixed argv list, never `shell=True`.

## Layout

- Flat modules, one concern each (see the module map in `README.md`). No packages, no framework.
- `pmtiler.py` is the entry point: serve, or `--selftest`.
- Each module's docstring states its concern, invariants and known ceilings. Keep it true when the module changes.

## Tests

- Every logic change adds an assertion to `--selftest` (`selftest.py`) that fails without the change.
- A change to the HTTP contract also updates `check.py`, and `python3 check.py --offline` passes.

## Configuration

- Config only via `PMTILER_*` env vars, read in `config.py`. No config files, no CLI flags beyond `--selftest`.
- Read env-derived values by attribute (`config.SPOOL`), never `from config import SPOOL`, so tests can reassign them.

## Public repo

- No host, deployment or operator specifics: hostnames, IPs, box sizing, allowlists, ansible or systemd unit names of a real deployment. Those live in infra. Generic examples in `examples/` are fine.

## Security

- Tag keys, values and geometry are attacker-authored. Never use data-derived strings as paths, shell input or HTML.
- Re-review [THREAT-MODEL.md](THREAT-MODEL.md) when a change fires one of its re-review triggers (new data-derived tile fields, a second consumer, auth or a public bind, tippecanoe replaced), and update its review date.

## Vocabulary

- Name things with the terms in [CONTEXT.md](CONTEXT.md). Respect its _Avoid_ lists in code, tests, commits and docs.
