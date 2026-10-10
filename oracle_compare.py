#!/usr/bin/env python3
"""Oracle compare: pmtiler convert() vs osmtogeojson on a captured fixture.

    python3 oracle_compare.py fixtures/delft-mixed.json

Runs the fixture through both converters and diffs feature counts by element
type, then relation geometries (type, ring count, vertex set -- rotation and
winding insensitive). osmtogeojson is resolved via npx from a neighbouring
checkout that has it (PMTILER_APP_DIR, default ../app). Exits nonzero on any
mismatch, printing at most CAP per kind.

Re-run whenever the pinned osmtogeojson target bumps (regenerate
POLYGON_FEATURES in convert.py from the new package's polygon-features.json
first) -- nothing else detects the two converters drifting apart.

fixtures/*.json captures are not in git (up to 11 MB), only the
.overpassql that produced them. Regenerate by POSTing the query to an
Overpass instance:

    curl -sk --data-urlencode data@fixtures/delft-mixed.overpassql \\
        -o fixtures/delft-mixed.json https://localhost:8080/api/interpreter

delft-mixed is the area-decision corpus: Delft highway/barrier/
man_made/aeroway, 14,518 ways -- where the closed non-area loops live. The
older delft-parks/amsterdam-parks captures predate query recording and are
all polygon-all keys, so they exercise relations but never the area ruleset.
"""
import json
import os
import subprocess
import sys

import pmtiler

CAP = 20  # printed mismatches per kind; a systematic divergence is 100k+ lines
# the app checkout supplies osmtogeojson; the default only holds for a sibling
# layout, so worktrees and other checkouts point PMTILER_APP_DIR at it instead
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("PMTILER_APP_DIR") or os.path.join(HERE, "..", "app")


def polys(geom):
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return list(geom["coordinates"])
    return []


def sig(geom):
    """Rotation/winding-insensitive polygon signature."""
    p = polys(geom)
    verts = frozenset((round(x, 7), round(y, 7))
                      for poly in p for ring in poly for x, y in ring)
    return (geom["type"], len(p), sum(len(poly) for poly in p), verts)


def main(fix):
    out = fix + ".ndjson"
    stats = pmtiler.convert(fix, out)
    ours, emitted = {}, 0
    with open(out) as f:
        for line in f:
            feat = json.loads(line)
            emitted += 1
            ours[feat["properties"]["@id"]] = feat["geometry"]

    raw = subprocess.run(["npx", "osmtogeojson", os.path.abspath(fix)],
                         cwd=APP, capture_output=True, text=True, check=True).stdout
    theirs = {f.get("id"): f["geometry"]
              for f in json.loads(raw)["features"]}

    fail = 0
    # keying by @id would silently hide double emission -- the one divergence
    # the converter documents (member node also matching the query) -- so
    # surface any duplicate ids explicitly instead
    if emitted != len(ours):
        print("DUPLICATE ids: %d features emitted, %d unique" % (emitted, len(ours)))
        fail = 1
    for kind in ("node", "way", "relation"):
        a = sum(1 for k in ours if k.startswith(kind + "/"))
        b = sum(1 for k in theirs if k and k.startswith(kind + "/"))
        mark = "ok" if a == b else "MISMATCH"
        if a != b:
            fail = 1
        print("%-9s ours=%-6d oracle=%-6d %s" % (kind, a, b, mark))
    only_theirs = sorted(k for k in theirs if k not in ours)
    only_ours = sorted(k for k in ours if k not in theirs)
    if only_theirs:
        print("only in oracle:", only_theirs[:20])
    if only_ours:
        print("only in ours:", only_ours[:20])

    # ways get a type-only check -- Polygon vs LineString is the whole point of the
    # tag-based area decision, and coordinates for a way are a straight copy of the
    # geometry Overpass sent. Relations additionally get the ring signature.
    type_bad, geom_bad, shared = [], [], 0
    for k in sorted(k for k in ours if k in theirs):
        shared += 1
        a, b = ours[k], theirs[k]
        if a["type"] != b["type"]:
            type_bad.append(k)
            if len(type_bad) <= CAP:
                print("type MISMATCH %s: ours %s, oracle %s"
                      % (k, a["type"], b["type"]))
        elif k.startswith("relation/") and sig(a) != sig(b):
            geom_bad.append(k)
            if len(geom_bad) <= CAP:
                print("geometry MISMATCH %s: ours %s %d polys, oracle %s %d polys"
                      % (k, a["type"], len(polys(a)), b["type"],
                         len(polys(b))))
    if type_bad or geom_bad:
        fail = 1
        print("%d type, %d ring mismatches of %d shared features"
              % (len(type_bad), len(geom_bad), shared))
    else:
        print("geometry types and relation rings match (%d compared)" % shared)
    print("relationsSkipped=%d skipped=%d" % (stats["relationsSkipped"],
                                              stats["skipped"]))
    return fail


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
