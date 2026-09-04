#!/usr/bin/env python3
"""Follow-up count probes: (label, relation, maxsize)."""
import json, subprocess, time

PROBES = [
    ("Tokyo Metropolis 3Gi", 1543125, 3221225472),
    ("Delhi NCT 1Gi", 1942586, 1073741824),
    ("Sao Paulo STATE 1Gi", 298204, 1073741824),
]

for label, rel, maxsize in PROBES:
    q = (f"[out:json][timeout:300][maxsize:{maxsize}];rel({rel});"
         'map_to_area -> .searchArea;'
         '(node["building"](area.searchArea);'
         'way["building"](area.searchArea);'
         'relation["building"](area.searchArea););out count;')
    t0 = time.time()
    out = subprocess.run(
        ["curl", "-sk", "https://localhost:8080/api/interpreter",
         "--data-urlencode", f"data={q}"],
        capture_output=True, text=True, timeout=320).stdout
    dt = time.time() - t0
    try:
        data = json.loads(out)
        remark = data.get("osm3s", {}).get("remark") or data.get("remark")
        if remark or not data.get("elements"):
            print(f"{label}: FAIL {dt:.0f}s: {(remark or 'no elements')[:160]}")
        else:
            c = int(data["elements"][0]["tags"]["total"])
            print(f"{label}: {c:,} in {dt:.0f}s (~{c*1039/1e9:.1f} GB wire)")
    except Exception:
        print(f"{label}: NON-JSON {dt:.0f}s: {out[:200]}")
