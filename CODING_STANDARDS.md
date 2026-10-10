# Coding Standards

How code in this repo should be written. Reviewers (human and agent) check changes against this file.

## Dependencies

- Python 3 standard library only. No `pip install`, no `requirements.txt`.
- `tippecanoe` on PATH is the only external tool. It is invoked with a fixed argv list, never `shell=True`.
- `check.py` may also call `pgrep` (procps) to assert no tippecanoe outlives an aborted job: the stdlib cannot list processes portably.

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

## Comments

- Names first. A comment that says what a value, function or block is means the name is wrong: rename and delete the comment.
- Comment only what code can't say: a non-obvious reason, a bug workaround, an outside constraint (Overpass behavior, tippecanoe quirk). One line by default.
- Module docstrings are the exception: they hold the concern, invariants and known ceilings (see Layout), and can run longer.
- No bare issue or PR refs (`#12`, `see #9`): they don't resolve where code is read. State the reason; use a full URL only if the ticket adds something the comment can't.

## Commits and PRs

- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`). Short, factual, no emojis.
- Issue-closing keywords (`Closes #N`) go in the PR body, never in commit messages.
- Branch from `main`, PR into `main`.
