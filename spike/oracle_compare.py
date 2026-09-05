#!/usr/bin/env python3
"""Oracle compare: pmtiler convert() vs osmtogeojson on a captured fixture.

    python3 spike/oracle_compare.py fixtures/delft-parks.json

Runs the fixture through both converters and diffs feature counts by element
type, then relation geometries (type, ring count, vertex set -- rotation and
winding insensitive). osmtogeojson is resolved via npx from the app checkout,
so the exact version the app depends on is the oracle. Exits nonzero on any
mismatch.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import pmtiler  # noqa: E402

APP = os.path.join(HERE, "..", "..", "app")


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

    geom_bad = []
    for k in sorted(k for k in ours if k.startswith("relation/")):
        if k not in theirs:
            continue
        if sig(ours[k]) != sig(theirs[k]):
            geom_bad.append(k)
            a, b = ours[k], theirs[k]
            print("geometry MISMATCH %s: ours %s %d polys, oracle %s %d polys"
                  % (k, a["type"], len(polys(a)), b["type"], len(polys(b))))
    if geom_bad:
        fail = 1
    else:
        rels = sum(1 for k in ours if k.startswith("relation/"))
        print("relation geometries match (%d compared)" % rels)
    print("relationsSkipped=%d skipped=%d" % (stats["relationsSkipped"],
                                              stats["skipped"]))
    return fail


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
