#!/usr/bin/env python3
"""Fast bounded-memory Overpass JSON -> nd-geojson. Splits the elements array on
top-level element boundaries by brace depth (line-based; Overpass pretty-prints),
then json.loads per element. Usage: convert_fast.py <in.json> <out.ndjson>"""
import datetime, json, resource, sys, time

inp, outp = sys.argv[1], sys.argv[2]
t0 = time.time()
stats = {"ways": 0, "nodes": 0, "skipped": 0}
editors, changesets = set(), set()

with open(inp, "r", encoding="utf-8") as f, open(outp, "w", encoding="utf-8") as out:
    # skip header until the elements array opens
    for line in f:
        if line.startswith('  "elements":'):
            break
    depth = 0
    buf = []
    for line in f:
        s = line.strip()
        if depth == 0:
            if s.startswith("{"):
                depth = s.count("{") - s.count("}")
                buf = [s]
                if depth == 0:
                    _el = s.rstrip(",")
                else:
                    continue
            elif s.startswith("]"):
                break
            else:
                continue
        else:
            buf.append(s)
            depth += s.count("{") - s.count("}")
            if depth > 0:
                continue
            _el = "".join(buf).rstrip(",")
        try:
            el = json.loads(_el)
        except json.JSONDecodeError:
            stats["skipped"] += 1
            continue
        tags = el.get("tags") or {}
        if el.get("user"):
            editors.add(el["user"])
        if el.get("changeset"):
            changesets.add(el["changeset"])
        geometry = None
        if el["type"] == "way" and el.get("geometry"):
            stats["ways"] += 1
            coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
            closed = len(coords) > 3 and coords[0] == coords[-1]
            geometry = ({"type": "Polygon", "coordinates": [coords]} if closed
                        else {"type": "LineString", "coordinates": coords})
        elif el["type"] == "node" and "lat" in el:
            stats["nodes"] += 1
            geometry = {"type": "Point", "coordinates": [el["lon"], el["lat"]]}
        else:
            stats["skipped"] += 1
            continue
        ts = el.get("timestamp", "")
        props = dict(tags)
        props["@id"] = f"{el['type']}/{el['id']}"
        props["_ts"] = (int(datetime.datetime.fromisoformat(
            ts.replace("Z", "+00:00")).timestamp()) if ts else 0)
        out.write(json.dumps({"type": "Feature", "properties": props,
                              "geometry": geometry}, separators=(",", ":")) + "\n")

peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // (1024 * 1024)
print(json.dumps({"seconds": round(time.time() - t0, 1), "peakRssMB": peak,
                  "features": stats["ways"] + stats["nodes"], **stats,
                  "editors": len(editors), "changesets": len(changesets)}))
