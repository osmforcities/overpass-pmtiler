# Coding Standards

How code in this repo should be written. Reviewers (human and agent) check changes against this file.

## Dependencies

- Python 3 standard library only. No `pip install`, no `requirements.txt`.
- `tippecanoe` on PATH, with the `tile-join` and `tippecanoe-decode` it ships, is the only external tool. All are invoked with a fixed argv list, never `shell=True`.
- `check.py` may also call `pgrep` (procps) to assert no tippecanoe outlives an aborted job: the stdlib cannot list processes portably.

## Layout

- Flat modules, one concern each. No packages, no framework.
- `pmtiler.py` is the entry point: serve, or `--selftest`.
- Each module's docstring states its concern, invariants and known ceilings. Keep it true when the module changes.

## Tests

- Every logic change adds an assertion to `--selftest` (`selftest.py`) that fails without the change.
- A change to the HTTP contract also updates `check.py`, and `python3 check.py --offline` passes.

## Configuration

- Config only via `PMTILER_*` env vars, read in `config.py`. No config files, no CLI flags beyond `--selftest`.
- Read env-derived values by attribute (`config.SPOOL`), never `from config import SPOOL`, so tests can reassign them.

## Public repo

- No host, deployment or operator specifics: hostnames, IPs, box sizing, allowlists, ansible or systemd unit names of a real deployment. Those stay out of this repo.

## Security

- Tag keys, values and geometry are attacker-authored. Never use data-derived strings as paths, shell input or HTML.
- Re-review [THREAT-MODEL.md](THREAT-MODEL.md) when a change fires one of its re-review triggers (new data-derived tile fields, a second consumer, auth or a public bind, tippecanoe replaced), and update its review date.

## Vocabulary

- Name things with the terms in [CONTEXT.md](CONTEXT.md). Respect its _Avoid_ lists in code, tests, commits and docs.

## Comments

Default is no comment. Names, `--selftest` assertions and ADRs carry meaning; comments rot. Before writing one, classify it:

- Tautology (repeats what the code shows): don't write it. Rename until the code says it.
- Excuse (explains a hack in our code, shortcut, "for now", "temporary", TODO, FIXME): don't write it. Fix the code, or keep it and file an issue. A workaround for an outside bug is a constraint, not an excuse (see below).
- Decision record (a non-obvious, lasting choice and its why): write an ADR in `docs/adr/` (create it with the first one), not a comment.
- Stale (describes code, behavior or history that no longer holds): delete it when your change touches it.

Allowed:

- Module docstrings (see Layout): concern, invariants, known ceilings. No decision history; that goes in an ADR.
- One line for an outside constraint the code can't express (Overpass behavior, tippecanoe quirk), with the source URL when there is one.
- Tool directives (`# noqa`) with the reason on the same line.

Never: commented-out code, section banners, function docstrings that restate the signature, changelog-style history ("was X, now Y"), bare issue or PR refs (`#12`, `see #9`: they don't resolve where code is read; use a full URL only if the ticket adds something the comment can't).

Forward-only: existing comments are not swept. A PR that touches a comment in a banned category deletes or moves it.

## Commits and PRs

- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`). Short, factual, no emojis.
- Issue-closing keywords (`Closes #N`) go in the PR body, never in commit messages.
- Branch from `main`, PR into `main`.
