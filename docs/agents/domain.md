# Domain Docs

How the engineering skills should consume this repo's domain documentation.

Layout: **single-context**. The root `CONTEXT.md` is the glossary.

## Before exploring, read these

- **`CONTEXT.md`**: the domain vocabulary, one line per term (`**Term**: definition. _Avoid:_ a, b.`).
- **`CODING_STANDARDS.md`**: how code here is written.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.

If a file or directory doesn't exist (there are no ADRs yet), proceed silently. New terms go in `CONTEXT.md`; the first ADR creates `docs/adr/0001-slug.md`.

## Use the glossary's vocabulary

When output names a domain concept (issue title, refactor proposal, hypothesis, test name), use the glossary term. Don't drift to the synonyms it lists under _Avoid_.

A concept missing from the glossary is a signal: either the language is invented (reconsider) or there is a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0001, but worth reopening because…_
