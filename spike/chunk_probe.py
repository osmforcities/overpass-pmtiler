#!/usr/bin/env python3
"""Chunked Overpass count probe. Usage: chunk_probe.py <relation_id> <grid_n> [maxsize_bytes]"""
import json, subprocess, sys, time

rel = sys.argv[1]
n = int(sys.argv[2])
maxsize = sys.argv[3] if len(sys.argv) > 3 else None

BBOXES = {
    "298285": (-24.0079003, -46.8262692, -23.3577551, -46.3650898),  # Sao Paulo
    "271110": None,  # Amsterdam: fetched on demand
}


def fetch_bbox(rel_id):
    q = f"[out:json][timeout:60];rel({rel_id});out ids bb;"
    out = subprocess.run(
        ["curl", "-sk", "https://localhost:8080/api/interpreter", "--data-urlencode", f"data={q}"],
        capture_output=True, text=True, timeout=90).stdout
    b = json.loads(out)["elements"][0]["bounds"]
    return (b["minlat"], b["minlon"], b["maxlat"], b["maxlon"])


bbox = BBOXES.get(rel) or fetch_bbox(rel)
s0, w0, n0, e0 = bbox
dlat = (n0 - s0) / n
dlon = (e0 - w0) / n

header = f"[out:json][timeout:180]"
if maxsize:
    header += f"[maxsize:{maxsize}]"

total = 0
fails = 0
counts = []
for i in range(n):
    for j in range(n):
        s, w = s0 + i * dlat, w0 + j * dlon
        nn, e = s + dlat, w + dlon
        bb = f"({s:.7f},{w:.7f},{nn:.7f},{e:.7f})"
        q = (f"{header};rel({rel});map_to_area -> .searchArea;"
             f'(node["building"](area.searchArea){bb};'
             f'way["building"](area.searchArea){bb};'
             f'relation["building"](area.searchArea){bb};);out count;')
        t0 = time.time()
        try:
            out = subprocess.run(
                ["curl", "-sk", "https://localhost:8080/api/interpreter", "--data-urlencode", f"data={q}"],
                capture_output=True, text=True, timeout=200).stdout
            dt = time.time() - t0
            data = json.loads(out)
            if "remark" in data.get("osm3s", {}) or not data.get("elements"):
                remark = data.get("osm3s", {}).get("remark") or data.get("remark") or out[:200]
                print(f"chunk[{i},{j}] FAIL {dt:.1f}s: {remark}")
                fails += 1
                continue
            c = int(data["elements"][0]["tags"]["total"])
        except Exception as ex:
            print(f"chunk[{i},{j}] ERROR {time.time()-t0:.1f}s: {ex}")
            fails += 1
            continue
        counts.append(c)
        total += c
        print(f"chunk[{i},{j}] {c:>9,} in {dt:.1f}s")

print(f"\ngrid {n}x{n} rel {rel} maxsize={maxsize or 'default(512Mi)'}")
print(f"sum={total:,}  chunks_ok={len(counts)}/{n*n}  fails={fails}")
if counts:
    print(f"max_chunk={max(counts):,}  mean={total//len(counts):,}  min={min(counts):,}")
