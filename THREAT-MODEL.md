# Threat model: can OSM-authored data hijack this service?

The question this document answers: anyone in the world can edit
OpenStreetMap, and this service ingests whatever they wrote. What can an
attacker reach by putting hostile content into tags, names, or geometry —
and what stops them?

Reviewed 2026-09-06 against the module split (post #29/#30/#31).

## Trust boundaries

```
OSM editors ──> planet ──> Overpass DB ──> [fetch] raw.json
 (attacker-               (trusted infra,      |
  editable)                runs the query)  [convert] data.ndjson + stats.json
                                               |
                                            [bake] tippecanoe -> output.pmtiles
                                               |
                                            [serve] HTTP (localhost + proxy
                                               |      allowlist)
                              consumers: the app, viewer.html, pmtiles clients
```

Attacker-controlled bytes are: tag keys and values (≤255 chars each in OSM,
but unbounded count), user names, and geometry/member structure. Everything
else (element ids, timestamps, versions, the JSON framing) is produced by
Overpass.

## Findings

### Closed by construction or by past fixes

- **Code execution via data** — no `eval`, no shell, no format strings over
  data anywhere. Elements are parsed with `json.loads` only; tippecanoe is
  invoked with a fixed argv list (never `shell=True`), and the only
  data-derived bytes it sees are JSON *string values* inside well-formed
  ndjson the converter itself serialized. Remaining exposure is a parser
  bug in tippecanoe itself (mature, widely-fuzzed C); running the service
  sandboxed with write access to its spool only bounds what such a bug
  could touch.
- **Path traversal** — job ids are charset-validated with `.`/`..` rejected
  (#31); output names go through the `OUTPUTS` allowlist; no data-derived
  string is ever used as a filesystem path.
- **Forged failure via a `remark` tag** — Overpass reports runtime errors
  as a top-level `"remark"` key; the detector is anchored to the
  pretty-printer's 2-space indent, and a JSON string cannot contain a
  literal newline, so a tag value cannot fabricate a top-level-looking
  line (#26, tested both ways in the selftest).
- **Splitter desync via braces in tag values** — the element splitter
  counts braces string-aware (#29, tested); before that fix a `{` in a tag
  value silently swallowed elements.
- **Log forging** — error text reaching `log()` comes from regex groups
  over JSON strings (no raw newlines) or from `html_error`, which strips
  tags and collapses whitespace.

### Fixed in this change

- **Viewer XSS via error text** — `viewer.html` rendered `job.error` with
  `innerHTML`. A tippecanoe error tail quotes the offending ndjson line,
  which contains attacker-authored tag values — so a crafted tag could run
  script in the operator's browser on the service origin (enough to call
  `DELETE /jobs/*` or exfiltrate stats). Error text and map-error messages
  are now inserted as text nodes. Stats rendering was already numbers-only.

### Accepted with bounds (known ceilings, not bugs)

- **Resource exhaustion via data growth** — an attacker bulk-editing an
  area watched by a consumer can inflate a fetch. Bounds: Overpass enforces
  `[maxsize:]` per query (service caps requests at 4 GiB); the spool is
  swept after `PMTILER_MAX_AGE_DAYS`; the serial queue means one job's
  damage at a time. A monster single element (a relation with tens of
  thousands of members) is buffered in RAM while parsed — element-sized,
  and itself inside the maxsize budget.
- **Content poisoning** — an attacker can put garbage *data* on the map
  (wrong names, junk geometry) and it will faithfully appear in tiles and
  stats. That is OSM vandalism, not a service compromise; the community
  reverts it and the next bake picks up the revert.

### Consumer obligations (out of this repo's hands)

- **Tile properties are raw OSM tag values by design** (osmtogeojson
  parity). Any consumer rendering them — map popups, filter panels, stats
  tables — must treat them as text, never HTML. React's default escaping
  is safe; MapLibre `Popup.setHTML` with tag content is not — use
  `setText` or build DOM. Same for `stats.json` values (`filterDimensions`
  carries tag values, `tagCounts` carries tag keys).
- **The HTTP surface has no auth by design.** Whatever limits who can
  reach the port is the access control: anyone who can
  reach the port can submit arbitrary Overpass queries, occupy the serial
  queue, and fill the spool. Keep the bind on localhost and the allowlist
  tight.

## Re-review triggers

Revisit this document when: the tile schema starts carrying new
data-derived fields, a second consumer appears, the service grows auth or
a public bind, or tippecanoe is replaced.
