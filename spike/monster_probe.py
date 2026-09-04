#!/usr/bin/env python3
"""Resolve candidate worst-case areas via Nominatim, count buildings via our Overpass."""
import json, subprocess, sys, time, urllib.parse

CANDIDATES = [
    "Tokyo, Japan",
    "Jakarta, Indonesia",
    "Delhi, India",
    "Mexico City, Mexico",
    "Cairo, Egypt",
    "Lagos, Nigeria",
    "Buenos Aires, Argentina",
    "Los Angeles, California",
    "New York City, New York",
    "Rio de Janeiro, Brazil",
    "Dar es Salaam, Tanzania",
    "Sao Paulo state, Brazil",
]

def nominatim(q):
    url = ("https://nominatim.openstreetmap.org/search?format=json&limit=3&q="
           + urllib.parse.quote(q))
    out = subprocess.run(
        ["curl", "-s", "-A", "osmforcities-capacity-spike/1.0", url],
        capture_output=True, text=True, timeout=30).stdout
    for r in json.loads(out):
        if r.get("osm_type") == "relation":
            return int(r["osm_id"]), r.get("display_name", "")[:70], r.get("type", "")
    return None, None, None

def count(rel):
    q = (f"[out:json][timeout:300][maxsize:1073741824];rel({rel});"
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
    except Exception:
        return None, dt, out[:150]
    remark = data.get("osm3s", {}).get("remark") or data.get("remark")
    if remark or not data.get("elements"):
        return None, dt, (remark or "no elements")[:150]
    return int(data["elements"][0]["tags"]["total"]), dt, None

print(f"{'query':<28} {'rel':>10} {'buildings':>12} {'time':>7}  note")
for q in CANDIDATES:
    rel, name, typ = nominatim(q)
    time.sleep(1.2)
    if not rel:
        print(f"{q:<28} {'—':>10} {'—':>12} {'—':>7}  nominatim: no relation")
        continue
    c, dt, err = count(rel)
    if c is None:
        print(f"{q:<28} {rel:>10} {'FAIL':>12} {dt:>6.0f}s  {err} | {name} ({typ})")
    else:
        gb = c * 1039 / 1e9  # measured avg bytes/element for buildings (SP: 2.23GB/2.14M)
        print(f"{q:<28} {rel:>10} {c:>12,} {dt:>6.0f}s  ~{gb:.1f} GB | {name} ({typ})")
