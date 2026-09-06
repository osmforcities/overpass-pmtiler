#!/usr/bin/env python3
"""overpass-pmtiler: turn an Overpass QL query into a PMTiles archive.

Serial job runner (one job at a time) + HTTP API, stdlib only. Job state is one
JSON file per job in a spool dir; no database. See README.md for the contract.

    python3 pmtiler.py             serve
    python3 pmtiler.py --selftest  converter self-check
"""
import datetime
import html
import http.server
import json
import math
import os
import queue
import re
import resource
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SPOOL = os.path.abspath(os.environ.get("PMTILER_SPOOL", "spool"))
OVERPASS = os.environ.get("PMTILER_OVERPASS", "http://localhost/api/interpreter")
PORT = int(os.environ.get("PMTILER_PORT", "8099"))
BIND = os.environ.get("PMTILER_BIND", "127.0.0.1")
INSECURE = os.environ.get("PMTILER_INSECURE") == "1"
NICE = os.environ.get("PMTILER_NICE", "10")
MAX_AGE_DAYS = float(os.environ.get("PMTILER_MAX_AGE_DAYS", "7"))

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_QUERY = 64 * 1024
MAX_MAXSIZE = 4 * 1024 ** 3
MAX_TIMEOUT = 3600
RUNNING = ("fetching", "converting", "baking")
OUTPUTS = {
    "output.pmtiles": "application/octet-stream",
    "stats.json": "application/json",
    "data.ndjson": "application/x-ndjson",
}

DAY = 86400
BANDS = (90 * DAY, 365 * DAY, 730 * DAY)  # emitted as stats recencyBandsDays
EARTH_R = 6371008.8  # turf's sphere radius: measures match @turf/length+area
MAX_FILTER_KEYS = 32

# osm-polygon-features 0.9.2, vendored (27 rules, no dependency): which tags make
# a closed way an area. key -> True (any value) | ("in"|"ex", frozenset(values)).
#
# Pin to the version osmtogeojson resolves (^0.9.1 -> 0.9.2), not to whatever
# is newest: osmtogeojson-compatible output is the contract here (see README
# "Compatibility targets"), so a newer table would break it rather than improve
# it. Upstream master is also 0.9.2 as of 2026-09, so the pin costs nothing
# today.
#
# Check rows against that package's polygon-features.json, never against recall
# -- the #12 review flagged railway's "platform" as a typo for "traverser",
# which appears in neither 0.9.2 nor upstream master. Regenerate only when the
# pinned compatibility target bumps, then re-run oracle_compare.py; nothing
# else detects the two drifting apart.
POLYGON_FEATURES = {
    "building": True,
    "highway": ("in", frozenset({"services", "rest_area", "escape", "elevator"})),
    "natural": ("ex", frozenset({"coastline", "cliff", "ridge", "arete", "tree_row"})),
    "landuse": True,
    "waterway": ("in", frozenset({"riverbank", "dock", "boatyard", "dam"})),
    "amenity": True,
    "leisure": True,
    "barrier": ("in", frozenset({"city_wall", "ditch", "hedge", "retaining_wall",
                                 "wall", "spikes"})),
    "railway": ("in", frozenset({"station", "turntable", "roundhouse", "platform"})),
    "area": True,
    "boundary": True,
    "man_made": ("ex", frozenset({"cutline", "embankment", "pipeline"})),
    "power": ("in", frozenset({"plant", "substation", "generator", "transformer"})),
    "place": True,
    "shop": True,
    "aeroway": ("ex", frozenset({"taxiway"})),
    "tourism": True,
    "historic": True,
    "public_transport": True,
    "office": True,
    "building:part": True,
    "military": True,
    "ruins": True,
    "area:highway": True,
    "craft": True,
    "golf": True,
    "indoor": True,
}

Q = queue.Queue()
CREATE_LOCK = threading.Lock()


class StageError(Exception):
    """Carries user-visible error text into the job's `failed` state."""


class Aborted(Exception):
    """The abort seam, in one place. DELETE removes the job dir; the running
    job notices on its next write_job (progress tick or state change), which
    raises this and unwinds the stage -- run_job exits quietly. A re-POST of
    a deleted id recreates the record with a fresh startedAt, so a stale
    run's write_job aborts on the generation mismatch instead of writing
    into the new job's directory. Stages abort only at write_job calls;
    stage_bake additionally kills its tippecanoe subprocess on unwind."""


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(*a):
    print(now_iso(), *a, flush=True)


# --- spool -----------------------------------------------------------------

def jobdir(jid):
    return os.path.join(SPOOL, jid)


def read_job(jid):
    try:
        with open(os.path.join(jobdir(jid), "job.json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:  # absent is normal (idempotency checks, 404s)
        return None
    except (OSError, ValueError) as e:
        # corrupt is not: without this line the job silently vanishes from
        # /jobs, recover() skips it forever and sweep waits MAX_AGE_DAYS
        log("unreadable job.json for", jid, "-", "%s: %s" % (type(e).__name__, e))
        return None


def write_job(job):
    # tmp + os.replace makes every read see a whole record: the reason
    # job.json needs no lock across threads. Never write it in place.
    jd = jobdir(job["id"])
    if not os.path.isdir(jd):
        raise Aborted(job["id"])
    current = read_job(job["id"])  # startedAt is the run's generation
    if current and current.get("startedAt") != job.get("startedAt"):
        raise Aborted(job["id"])  # dir was deleted and recreated under us
    job["updatedAt"] = now_iso()
    path = os.path.join(jd, "job.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f)
    os.replace(tmp, path)


def set_state(job, state):
    job["state"] = state
    job.pop("progress", None)
    write_job(job)
    log("job", job["id"], "->", state)


def progress_writer(job, stage, interval=1.0):
    """Throttled progress into job.json -- callers poll state, nothing is
    pushed. Each write is also an abort checkpoint (see Aborted)."""
    last = [0.0]

    def tick(**fields):
        if time.time() - last[0] < interval:
            return
        last[0] = time.time()
        job["progress"] = dict(stage=stage, **fields)
        write_job(job)

    return tick


def sweep():
    cutoff = time.time() - MAX_AGE_DAYS * DAY
    for name in os.listdir(SPOOL):
        path = os.path.join(SPOOL, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                log("swept", name)
        except OSError:
            pass


def recover():
    """Crash recovery: running states go back to queued, then re-enqueue FIFO."""
    pending = []
    for name in os.listdir(SPOOL):
        job = read_job(name)
        if not job:
            continue
        if job.get("state") in RUNNING:
            set_state(job, "queued")  # via set_state so stale progress clears
            log("recovered", name)
        if job.get("state") == "queued":
            pending.append(job)
    for job in sorted(pending, key=lambda j: j.get("startedAt") or ""):
        Q.put(job["id"])


# --- fetch -----------------------------------------------------------------

SETTING_RE = re.compile(r"\[\s*(?:maxsize|timeout)\s*:\s*\d+\s*\]", re.I)
# anchored to the pretty-printer's top-level indent: an element's remark=* TAG
# sits deeper and must not fail a successful fetch
REMARK_RE = re.compile(r'^  "remark"\s*:\s*"((?:[^"\\]|\\.)*)"', re.M)
TAG_RE = re.compile(r"<[^>]+>")


def apply_settings(query, maxsize, timeout):
    """Merge [maxsize:]/[timeout:] into the query's leading settings statement."""
    q = query.strip()
    extra = ""
    if maxsize:
        extra += "[maxsize:%d]" % maxsize
    if timeout:
        extra += "[timeout:%d]" % timeout
    if not extra:
        return q
    end = q.find(";")
    if q.startswith("[") and end != -1:
        return SETTING_RE.sub("", q[:end]) + extra + q[end:]
    return "[out:json]" + extra + ";" + q


def html_error(body):
    """Overpass returns its error text inside an HTML page, after boilerplate."""
    text = re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", body))).strip()
    m = re.search(r"Error\s*:", text)  # tag stripping leaves "<b>Error</b>:" spaced
    if m:
        text = text[m.start():]
    return text[:2000] or "overpass returned an error"


def tail_remark(path):
    """Overpass reports runtime errors as a "remark" key in an otherwise-200 body."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - 8192))
        tail = f.read().decode("utf-8", "replace")
    m = REMARK_RE.search(tail)
    return m.group(1) if m else None


def stage_fetch(job, path):
    query = apply_settings(job["query"], job.get("maxsize"), job.get("timeout"))
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(
        OVERPASS, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    ctx = ssl._create_unverified_context() if INSECURE else None
    tick = progress_writer(job, "fetching")
    try:
        with urllib.request.urlopen(
                req, context=ctx, timeout=(job.get("timeout") or 180) + 60) as r, \
                open(path, "wb") as out:
            got = 0
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                # no total: Overpass answers chunked, there is no Content-Length
                tick(bytes=got)
    except urllib.error.HTTPError as e:
        raise StageError(html_error(e.read().decode("utf-8", "replace")))
    except urllib.error.URLError as e:
        raise StageError("overpass unreachable: %s" % (e.reason,))
    except TimeoutError:
        raise StageError("overpass read timed out after %ds"
                         % ((job.get("timeout") or 180) + 60))
    remark = tail_remark(path)
    if remark:
        raise StageError(remark)
    log("fetched", job["id"], os.path.getsize(path), "bytes")


# --- convert ---------------------------------------------------------------

def is_area(tags):
    """Do these tags make a closed way an area? osmtogeojson's _isPolygonFeature,
    mirrored -- the purely geometric test fills roundabouts and closed cycleway
    loops, diverging from what consumers migrating off client-side conversion
    already render. The key "area" is itself a polygon-features key, so area=yes
    falls out of the generic loop below; only area=no needs a special case.
    """
    if tags.get("area") == "no":
        return False
    for key, val in tags.items():
        rule = POLYGON_FEATURES.get(key)
        if rule is None or val == "no":  # building=no is not an area
            continue
        if rule is True:
            return True
        mode, values = rule
        if (val in values) if mode == "in" else (val not in values):
            return True
    return False


def ring_contains(ring, pt):
    """Ray cast: point inside closed ring? Boundary hits are either/or, which is
    fine for assigning inner rings -- they sit strictly inside their outer."""
    x, y = pt
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def stitch(segments):
    """Join way fragments into closed rings.

    Fragments arrive in arbitrary order and direction, so grow each ring by
    endpoint lookup, reversing segments as needed. Fragments that never close
    are dropped -- broken relations are routine in real OSM data.
    """
    rings, opens = [], []
    for s in segments:
        if len(s) >= 4 and s[0] == s[-1]:
            rings.append(s)
        elif len(s) >= 2:
            opens.append(s)
    ends = {}
    for i, s in enumerate(opens):
        ends.setdefault(tuple(s[0]), []).append(i)
        ends.setdefault(tuple(s[-1]), []).append(i)
    used = [False] * len(opens)
    for i, seg in enumerate(opens):
        if used[i]:
            continue
        used[i] = True
        ring = list(seg)
        while ring[0] != ring[-1]:
            # ponytail: greedy pick when 3+ fragments meet at one endpoint --
            # touching rings can mis-stitch. Backtracking join if fixtures hit it.
            j = next((k for k in ends.get(tuple(ring[-1]), []) if not used[k]), None)
            if j is None:
                break
            used[j] = True
            nxt = opens[j]
            ring.extend(nxt[1:] if nxt[0] == ring[-1] else reversed(nxt[:-1]))
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def assemble_relation(el):
    """type=multipolygon/boundary -> (geometry, flat_coords, member_nodes);
    any other type or an unassemblable relation -> None (callers count it;
    never crash a bake). The blanket except below also turns coding bugs into
    relationsSkipped -- oracle_compare.py is the detector for that.

    Members carry inline geometry under `out geom`, so the element is
    self-contained. Member ways are consumed for ring assembly only, never
    emitted -- a way appears top-level iff it matched the query itself.
    Member nodes (label, admin_centre) come back as (id, lon, lat) and ARE
    emitted as bare Points: osmtogeojson does the same, and count parity with
    osmtogeojson is the contract. Known freak divergence: a
    member node that also matched the query top-level emits twice here where
    osmtogeojson dedups by id -- streaming cannot know without holding every
    node id seen.
    """
    try:
        if (el.get("tags") or {}).get("type") not in ("multipolygon", "boundary"):
            return None
        outers, inners, member_nodes = [], [], []
        for m in el.get("members") or []:
            if m.get("type") == "node" and "lat" in m:
                member_nodes.append((m.get("ref"), m["lon"], m["lat"]))
                continue
            if m.get("type") != "way" or not m.get("geometry"):
                continue
            coords = [[p["lon"], p["lat"]] for p in m["geometry"]]
            role = m.get("role") or "outer"
            if role == "outer":
                outers.append(coords)
            elif role == "inner":
                inners.append(coords)
        polys = [[o] for o in stitch(outers)]
        if not polys:
            return None
        for ring in stitch(inners):  # inner with no containing outer: dropped
            # ponytail: first containing outer wins -- nested outers (island in
            # lake in island) can misassign the hole. Smallest-containing if hit.
            for poly in polys:
                if ring_contains(poly[0], ring[0]):
                    poly.append(ring)
                    break
        geometry = ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
                    else {"type": "MultiPolygon", "coordinates": polys})
        flat = [pt for poly in polys for ring in poly for pt in ring]
        flat += [[lon, lat] for _, lon, lat in member_nodes]
        return geometry, flat, member_nodes
    except Exception:
        return None


def line_length_m(coords):
    """Haversine sum over a coordinate string -- @turf/length parity."""
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
             + math.sin(math.radians(lon2 - lon1) / 2) ** 2
             * math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)))
        total += 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)) * EARTH_R
    return total


def ring_area_m2(ring):
    """Chamberlain-Duquette spherical excess (signed) -- @turf/area parity."""
    n = len(ring)
    if n <= 2:
        return 0.0
    total = 0.0
    for i in range(n):
        total += ((math.radians(ring[(i + 2) % n][0]) - math.radians(ring[i][0]))
                  * math.sin(math.radians(ring[(i + 1) % n][1])))
    return total * EARTH_R * EARTH_R / 2


def polygons_area_m2(polys):
    """Sum of per-polygon (|outer| - sum |inners|), turf's Polygon handling."""
    total = 0.0
    for rings in polys:
        total += abs(ring_area_m2(rings[0]))
        for hole in rings[1:]:
            total -= abs(ring_area_m2(hole))
    return total


def tag_dimension(key, counts, casing, missing):
    """FilterDimension for one key: values sorted desc by count (ties keep
    first-seen order), displayed with the dominant original casing (ties
    first-seen), zero-value keys kept (keepEmpty semantics)."""
    values = [{"value": max(casing[lo].items(), key=lambda vc: vc[1])[0],
               "count": c}
              for lo, c in sorted(counts.items(), key=lambda lc: -lc[1])]
    return {"key": key, "kind": "tag", "values": values, "missing": missing}


def convert(inp, outp, tick=None, filter_keys=()):
    """Overpass JSON -> nd-geojson + accumulator stats in one bounded-memory pass.

    Splits the elements array on element boundaries by brace depth (line-based;
    Overpass pretty-prints), then json.loads per element: 13 MB RSS at 2.23 GB in.
    Ported from spike/convert_fast.py.
    """
    t0 = time.time()
    now = int(time.time())
    total = os.path.getsize(inp)  # the input is complete here, so pct is honest
    read = 0
    counts = {"nodes": 0, "ways": 0, "relations": 0,
              "relationsSkipped": 0, "skipped": 0}
    editors, changesets = set(), set()
    recent_editors, recent_changesets = set(), set()
    mapper_latest, tag_counts = {}, {}
    edit_bands = [0, 0, 0, 0]
    version_sum = age_sum = dated = recent_edits = stale = fresh = 0
    oldest = newest = None
    bbox = [180.0, 90.0, -180.0, -90.0]
    dim_counts = {k: {} for k in filter_keys}   # key -> lowercased value -> count
    dim_casing = {k: {} for k in filter_keys}   # key -> lower -> {variant: count}
    dim_missing = dict.fromkeys(filter_keys, 0)
    mix = {"points": 0, "lines": 0, "areas": 0, "lineKm": 0.0, "areaKm2": 0.0}

    # read binary: len(line) is then exact bytes, and json.loads takes bytes, so
    # progress costs nothing (f.tell() is disabled while iterating a text file)
    with open(inp, "rb") as f, open(outp, "w", encoding="utf-8") as out:
        for line in f:  # skip the header, stop at the elements array
            read += len(line)
            if line.startswith(b'  "elements":'):
                break
        depth = 0
        buf = []
        for line in f:
            read += len(line)
            s = line.strip()
            if depth == 0:
                if s.startswith(b"{"):
                    depth = s.count(b"{") - s.count(b"}")
                    buf = [s]
                    if depth:
                        continue
                    raw = s.rstrip(b",")
                elif s.startswith(b"]"):
                    break
                else:
                    continue
            else:
                buf.append(s)
                depth += s.count(b"{") - s.count(b"}")
                if depth > 0:
                    continue
                raw = b"".join(buf).rstrip(b",")
            try:
                el = json.loads(raw)
            except ValueError:
                counts["skipped"] += 1
                continue

            kind = el.get("type")
            tags = el.get("tags") or {}
            member_nodes = ()
            if kind == "way" and el.get("geometry"):
                coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
                # ponytail: closure by coordinate, not node id as osmtogeojson
                # does -- out geom carries no ids. Degenerate 3-coord rings stay
                # LineStrings here; the >3 guard also protects the ring math.
                closed = (len(coords) > 3 and coords[0] == coords[-1]
                          and is_area(tags))
                geometry = ({"type": "Polygon", "coordinates": [coords]} if closed
                            else {"type": "LineString", "coordinates": coords})
                counts["ways"] += 1
            elif kind == "node" and "lat" in el:
                coords = [[el["lon"], el["lat"]]]
                geometry = {"type": "Point", "coordinates": coords[0]}
                counts["nodes"] += 1
            elif kind == "relation":
                assembled = assemble_relation(el)
                if not assembled:
                    counts["relationsSkipped"] += 1
                    continue
                geometry, coords, member_nodes = assembled
                counts["relations"] += 1
            else:
                counts["skipped"] += 1
                continue

            gt = geometry["type"]
            if gt == "Point":
                mix["points"] += 1
            elif gt == "LineString":
                mix["lines"] += 1
                mix["lineKm"] += line_length_m(coords) / 1000.0
            else:
                mix["areas"] += 1
                mix["areaKm2"] += polygons_area_m2(
                    geometry["coordinates"] if gt == "MultiPolygon"
                    else [geometry["coordinates"]]) / 1e6

            for lon, lat in coords:
                if lon < bbox[0]:
                    bbox[0] = lon
                if lat < bbox[1]:
                    bbox[1] = lat
                if lon > bbox[2]:
                    bbox[2] = lon
                if lat > bbox[3]:
                    bbox[3] = lat

            user = el.get("user")
            if user:
                editors.add(user)
            if el.get("changeset"):
                changesets.add(el["changeset"])
            version_sum += el.get("version") or 0

            ts_iso = el.get("timestamp") or ""
            ts = 0
            if ts_iso:
                ts = int(datetime.datetime.fromisoformat(
                    ts_iso.replace("Z", "+00:00")).timestamp())
                age = now - ts
                dated += 1
                age_sum += age
                oldest = ts if oldest is None or ts < oldest else oldest
                newest = ts if newest is None or ts > newest else newest
                edit_bands[sum(age > b for b in BANDS)] += 1
                if age <= BANDS[0]:
                    recent_edits += 1
                    if user:
                        recent_editors.add(user)
                    if el.get("changeset"):
                        recent_changesets.add(el["changeset"])
                if age > BANDS[2]:
                    stale += 1
                if age <= BANDS[1]:
                    fresh += 1
                if user and ts > mapper_latest.get(user, 0):
                    mapper_latest[user] = ts

            for key in tags:
                tag_counts[key] = tag_counts.get(key, 0) + 1

            for key in filter_keys:
                raw = tags.get(key)
                if raw is None:
                    dim_missing[key] += 1
                    continue
                # ";"-split into separate buckets (vending=drinks;food); quirk
                # kept on purpose: all-empty tokens fall back to the raw value
                tokens = [t for t in (p.strip() for p in raw.split(";")) if t] \
                    or [raw]
                for value in tokens:
                    lower = value.lower()
                    kc = dim_counts[key]
                    kc[lower] = kc.get(lower, 0) + 1
                    variants = dim_casing[key].setdefault(lower, {})
                    variants[value] = variants.get(value, 0) + 1

            props = dict(tags)
            props["@id"] = "%s/%s" % (kind, el["id"])
            props["_ts"] = ts  # numeric epoch: a string breaks MapLibre `step`
            out.write(json.dumps({"type": "Feature", "properties": props,
                                  "geometry": geometry},
                                 separators=(",", ":")) + "\n")
            for ref, lon, lat in member_nodes:  # bare Points, osmtogeojson parity
                counts["nodes"] += 1
                mix["points"] += 1
                for key in filter_keys:  # tagless, so missing for every dimension
                    dim_missing[key] += 1
                out.write(json.dumps(
                    {"type": "Feature",
                     "properties": {"@id": "node/%s" % ref, "_ts": 0},
                     "geometry": {"type": "Point", "coordinates": [lon, lat]}},
                    separators=(",", ":")) + "\n")
            if tick:
                tick(features=counts["nodes"] + counts["ways"] + counts["relations"],
                     bytes=read, total=total, pct=round(100.0 * read / total, 1))

    features = counts["nodes"] + counts["ways"] + counts["relations"]
    mapper_bands = [0, 0, 0, 0]
    for ts in mapper_latest.values():
        mapper_bands[sum((now - ts) > b for b in BANDS)] += 1
    pct = (lambda n: round(100.0 * n / features, 2)) if features else (lambda n: 0.0)
    return {
        "schemaVersion": 1,  # cross-repo contract: bump on removals/meaning changes
        "recencyBandsDays": [b // DAY for b in BANDS],
        "features": features,
        **counts,
        "editorsCount": len(editors),
        "changesetsCount": len(changesets),
        "elementVersionsCount": version_sum,
        "oldestElement": iso(oldest),
        "mostRecentElement": iso(newest),
        "averageElementAge": round(age_sum / dated / DAY, 2) if dated else None,
        "averageElementVersion": round(version_sum / features, 2) if features else None,
        "recentActivity": {"elementsEdited": recent_edits,
                           "changesets": len(recent_changesets),
                           "editors": len(recent_editors)},
        "qualityMetrics": {"staleElementsCount": stale,
                           "recentlyUpdatedElementsCount": fresh,
                           "staleElementsPercentage": pct(stale),
                           "recentlyUpdatedElementsPercentage": pct(fresh)},
        "editRecencyBands": edit_bands,
        "mapperRecencyBands": mapper_bands,
        "tagCounts": [{"key": k, "count": c} for k, c in
                      sorted(tag_counts.items(), key=lambda kv: -kv[1])],
        "filterDimensions": [
            tag_dimension(k, dim_counts[k], dim_casing[k], dim_missing[k])
            for k in filter_keys],
        "geometryMix": mix,
        "bbox": bbox if features else None,
        "seconds": round(time.time() - t0, 1),
        "peakRssMB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // (1024 * 1024),
    }


def iso(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# --- bake ------------------------------------------------------------------

def validate_pmtiles(path):
    """PMTiles v3 header: magic, version, and a nonzero addressed-tile count."""
    with open(path, "rb") as f:
        head = f.read(127)
    if len(head) < 127 or head[:7] != b"PMTiles" or head[7] != 3:
        raise StageError("bake produced an unreadable pmtiles archive")
    if int.from_bytes(head[72:80], "little") == 0:
        raise StageError("bake produced an archive with no tiles")


PROGRESS_RE = re.compile(r'^\{"progress":\s*([\d.]+)\}$')


def stage_bake(job, jd):
    out = os.path.join(jd, "output.pmtiles")
    cmd = ["nice", "-n", NICE, "tippecanoe", "-f", "-zg",
           "--drop-densest-as-needed", "-P", "-l", "features",
           # --json-progress replaces the \r-redrawn bar with one line per tick,
           # and leaves real errors on stderr untouched
           "--json-progress", "--progress-interval=1",
           "-o", out, os.path.join(jd, "data.ndjson")]
    # tmp files stay inside the job dir, never /tmp
    p = subprocess.Popen(cmd, cwd=jd, env=dict(os.environ, TMPDIR=jd),
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True)
    tick = progress_writer(job, "baking")
    errors = []
    try:
        for line in p.stderr:  # tippecanoe throttles, so every line is used
            m = PROGRESS_RE.match(line.strip())
            if m:
                tick(pct=float(m.group(1)))
            elif line.strip():
                errors.append(line.rstrip())
                del errors[:-20]
        if p.wait() != 0:
            raise StageError(
                "tippecanoe: " + ("\n".join(errors)[-2000:] or "failed"))
    finally:
        if p.poll() is None:  # abort mid-bake: don't orphan the subprocess
            p.kill()
            p.wait()
    validate_pmtiles(out)
    log("baked", job["id"], os.path.getsize(out), "bytes")


# --- runner ----------------------------------------------------------------

def run_job(jid):
    job = read_job(jid)
    if not job:
        return
    jd = jobdir(jid)
    raw = os.path.join(jd, "raw.json")
    try:
        set_state(job, "fetching")
        stage_fetch(job, raw)

        set_state(job, "converting")
        stats = convert(raw, os.path.join(jd, "data.ndjson"),
                        progress_writer(job, "converting"),
                        filter_keys=job.get("filterableTags") or [])
        with open(os.path.join(jd, "stats.json"), "w", encoding="utf-8") as f:
            json.dump(stats, f)
        job["stats"] = stats
        os.remove(raw)  # ~2 GB at metro scale; the nd-geojson supersedes it
        if not stats["features"]:
            # fail here with the real cause; tippecanoe would exit nonzero
            # with "Did not read any valid geometries" two stages later
            raise StageError("query matched no elements")

        set_state(job, "baking")
        stage_bake(job, jd)
        set_state(job, "done")
    except Aborted:
        log("job", jid, "aborted (deleted)")
    except StageError as e:
        fail(job, str(e))
    except Exception as e:
        fail(job, "%s: %s" % (type(e).__name__, e))


def fail(job, error):
    # keep the run's own dict: a re-read would adopt the on-disk record's
    # generation and stomp a re-POSTed job (write_job couldn't tell them apart)
    job["error"] = error
    try:
        set_state(job, "failed")
    except Aborted:
        pass
    log("job", job["id"], "failed:", error[:200])


def worker():
    while True:
        jid = Q.get()
        try:
            run_job(jid)
            sweep()
        except Exception as e:
            # a sweep()/fail() error must not kill the only worker thread --
            # the HTTP surface would keep queueing jobs nobody runs
            log("worker error on", jid, "-", "%s: %s" % (type(e).__name__, e))
        finally:
            Q.task_done()


# --- http ------------------------------------------------------------------

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pmtiler/0.1"
    protocol_version = "HTTP/1.1"

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def err(self, status, message):
        self.send_json(status, {"error": message})

    def parse(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if not parts or parts[0] != "jobs":
            return None, None
        jid = parts[1] if len(parts) > 1 else None
        if jid is not None and not ID_RE.match(jid):
            return None, None
        return jid, parts[2] if len(parts) > 2 else None

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/jobs":
            return self.err(404, "not found")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_QUERY + 4096:
            return self.err(413, "body too large")
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.err(400, "invalid json")

        jid = req.get("id")
        query = req.get("query")
        if not isinstance(jid, str) or not ID_RE.match(jid):
            return self.err(400, "id must match [A-Za-z0-9._-]{1,128}")
        if not isinstance(query, str) or not query.strip():
            return self.err(400, "query is required")
        if len(query) > MAX_QUERY:
            return self.err(400, "query exceeds %d bytes" % MAX_QUERY)
        try:
            maxsize = int(req["maxsize"]) if req.get("maxsize") is not None else None
            timeout = int(req["timeout"]) if req.get("timeout") is not None else None
        except (TypeError, ValueError):
            return self.err(400, "maxsize and timeout must be integers")
        if maxsize is not None and not 0 < maxsize <= MAX_MAXSIZE:
            return self.err(400, "maxsize out of range")
        if timeout is not None and not 0 < timeout <= MAX_TIMEOUT:
            return self.err(400, "timeout out of range")
        ftags = req.get("filterableTags")
        if ftags is not None and not (
                isinstance(ftags, list) and len(ftags) <= MAX_FILTER_KEYS
                and all(isinstance(k, str) and 0 < len(k) <= 255 for k in ftags)):
            return self.err(400, "filterableTags must be a list of up to %d "
                            "tag keys" % MAX_FILTER_KEYS)

        with CREATE_LOCK:
            existing = read_job(jid)
            if existing:  # idempotent resubmit: report state, never re-run
                return self.send_json(200, {"id": jid, "state": existing["state"]})
            os.makedirs(jobdir(jid), exist_ok=True)
            write_job({"id": jid, "query": query, "maxsize": maxsize,
                       "timeout": timeout, "filterableTags": ftags,
                       "state": "queued",
                       "startedAt": now_iso(), "error": None, "stats": None})
            Q.put(jid)
        log("job", jid, "queued")
        self.send_json(202, {"id": jid, "state": "queued"})

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/viewer.html"):  # visual inspection, same-origin
            return self.send_file(os.path.join(HERE, "viewer.html"), "text/html")
        if path == "/jobs":
            # .get() throughout: one malformed job.json must not 500 the list
            return self.send_json(200, sorted(
                ({"id": j.get("id"), "state": j.get("state"),
                  "startedAt": j.get("startedAt"), "progress": j.get("progress")}
                 for j in filter(None, map(read_job, os.listdir(SPOOL)))),
                key=lambda j: j["startedAt"] or "", reverse=True))
        jid, name = self.parse()
        if not jid:
            return self.err(404, "not found")
        job = read_job(jid)
        if not job:
            return self.err(404, "no such job")
        if name is None:
            view = {"id": jid, "state": job["state"], "startedAt": job["startedAt"]}
            if job.get("progress"):
                view["progress"] = job["progress"]
            if job.get("error"):
                view["error"] = job["error"]
            if job.get("stats"):
                view["stats"] = job["stats"]
            return self.send_json(200, view)
        if name not in OUTPUTS:
            return self.err(404, "not found")
        path = os.path.join(jobdir(jid), name)
        if not os.path.isfile(path):
            return self.err(404, "%s not ready (state %s)" % (name, job["state"]))
        self.send_file(path, OUTPUTS[name])

    do_HEAD = do_GET

    def do_DELETE(self):
        jid, name = self.parse()
        if not jid or name:
            return self.err(404, "not found")
        if not os.path.isdir(jobdir(jid)):
            return self.err(404, "no such job")
        shutil.rmtree(jobdir(jid), ignore_errors=True)
        log("job", jid, "deleted")
        self.send_json(200, {"id": jid, "state": "deleted"})

    def send_file(self, path, ctype):
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        # ponytail: single range only; a multi-range request gets the full body.
        m = RANGE_RE.fullmatch((self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            while length:
                chunk = f.read(min(1 << 20, length))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve():
    os.makedirs(SPOOL, exist_ok=True)
    sweep()
    recover()
    threading.Thread(target=worker, daemon=True).start()
    httpd = Server((BIND, PORT), Handler)
    log("pmtiler on http://%s:%d spool=%s overpass=%s"
        % (BIND, httpd.server_address[1], SPOOL, OVERPASS))
    httpd.serve_forever()


# --- selftest --------------------------------------------------------------

FIXTURE = '''{
  "version": 0.6,
  "elements": [

{
  "type": "way",
  "id": 1,
  "timestamp": "2026-08-01T00:00:00Z",
  "version": 3,
  "changeset": 11,
  "user": "ada",
  "nodes": [1,2,3,1],
  "geometry": [
    { "lat": 0.0, "lon": 0.0 },
    { "lat": 0.0, "lon": 1.0 },
    { "lat": 1.0, "lon": 1.0 },
    { "lat": 0.0, "lon": 0.0 }
  ],
  "tags": { "building": "yes", "surface": "Asphalt;gravel" }
},
{
  "type": "way",
  "id": 2,
  "timestamp": "2019-01-01T00:00:00Z",
  "version": 1,
  "changeset": 12,
  "user": "bob",
  "geometry": [ { "lat": 2.0, "lon": 2.0 }, { "lat": 3.0, "lon": 3.0 } ],
  "tags": { "highway": "path", "building": "no" }
},
{ "type": "node", "id": 3, "lat": -1.0, "lon": -1.0, "version": 2,
  "changeset": 11, "user": "ada", "timestamp": "2026-08-02T00:00:00Z",
  "tags": { "surface": "asphalt" } },
{ "type": "relation", "id": 4, "members": [], "tags": { "building": "yes" } },
{
  "type": "relation",
  "id": 5,
  "timestamp": "2026-08-03T00:00:00Z",
  "version": 2,
  "changeset": 13,
  "user": "ada",
  "members": [
    {
      "type": "way",
      "ref": 10,
      "role": "outer",
      "geometry": [
        { "lat": 10.0, "lon": 20.0 },
        { "lat": 10.0, "lon": 21.0 },
        { "lat": 11.0, "lon": 21.0 }
      ]
    },
    {
      "type": "way",
      "ref": 11,
      "role": "outer",
      "geometry": [
        { "lat": 10.0, "lon": 20.0 },
        { "lat": 11.0, "lon": 20.0 },
        { "lat": 11.0, "lon": 21.0 }
      ]
    },
    {
      "type": "way",
      "ref": 12,
      "role": "inner",
      "geometry": [
        { "lat": 10.4, "lon": 20.4 },
        { "lat": 10.4, "lon": 20.6 },
        { "lat": 10.6, "lon": 20.6 },
        { "lat": 10.6, "lon": 20.4 },
        { "lat": 10.4, "lon": 20.4 }
      ]
    },
    { "type": "node", "ref": 99, "role": "label", "lat": 10.5, "lon": 20.5 }
  ],
  "tags": { "type": "multipolygon", "leisure": "park" }
},
{
  "type": "relation",
  "id": 6,
  "members": [
    {
      "type": "way",
      "ref": 13,
      "role": "outer",
      "geometry": [
        { "lat": 12.0, "lon": 22.0 },
        { "lat": 12.0, "lon": 22.5 },
        { "lat": 12.5, "lon": 22.5 },
        { "lat": 12.0, "lon": 22.0 }
      ]
    },
    {
      "type": "way",
      "ref": 14,
      "role": "outer",
      "geometry": [
        { "lat": 13.0, "lon": 23.0 },
        { "lat": 13.0, "lon": 23.5 },
        { "lat": 13.5, "lon": 23.5 },
        { "lat": 13.0, "lon": 23.0 }
      ]
    }
  ],
  "tags": { "type": "multipolygon", "natural": "water" }
},
{
  "type": "relation",
  "id": 7,
  "members": [
    {
      "type": "way",
      "ref": 15,
      "role": "outer",
      "geometry": [
        { "lat": 14.0, "lon": 24.0 },
        { "lat": 14.0, "lon": 24.5 },
        { "lat": 14.5, "lon": 24.5 },
        { "lat": 14.0, "lon": 24.0 }
      ]
    }
  ],
  "tags": { "type": "boundary", "boundary": "administrative" }
},
{
  "type": "relation",
  "id": 8,
  "members": [
    {
      "type": "way",
      "ref": 16,
      "role": "",
      "geometry": [ { "lat": 15.0, "lon": 25.0 }, { "lat": 15.1, "lon": 25.1 } ]
    }
  ],
  "tags": { "type": "route", "route": "bicycle" }
},
{
  "type": "relation",
  "id": 9,
  "members": [
    {
      "type": "way",
      "ref": 17,
      "role": "outer",
      "geometry": [ { "lat": 16.0, "lon": 26.0 }, { "lat": 16.1, "lon": 26.1 } ]
    }
  ],
  "tags": { "type": "multipolygon", "landuse": "forest" }
}

  ]
}
'''


def selftest():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".selftest")
    os.makedirs(d, exist_ok=True)
    try:
        src, dst = os.path.join(d, "raw.json"), os.path.join(d, "out.ndjson")
        with open(src, "w", encoding="utf-8") as f:
            f.write(FIXTURE)
        ticks = []
        s = convert(src, dst, lambda **kw: ticks.append(kw),  # unthrottled
                    filter_keys=["surface", "building", "nonexistent"])
        with open(dst, encoding="utf-8") as f:
            feats = [json.loads(line) for line in f]

        # convert knows its denominator: the input file is complete by now
        # (relation 5 ticks once but adds two features: itself + member node)
        assert len(ticks) == 6, ticks
        assert [t["features"] for t in ticks] == [1, 2, 3, 5, 6, 7], ticks
        assert all(t["total"] == os.path.getsize(src) for t in ticks), ticks
        assert [t["bytes"] for t in ticks] == sorted(t["bytes"] for t in ticks)
        # < 100 at the last feature: the skipped relations and the closing
        # brackets are still ahead of the read cursor
        assert 60 < ticks[-1]["pct"] < 100, ticks[-1]

        assert [f["geometry"]["type"] for f in feats] == \
            ["Polygon", "LineString", "Point",
             "Polygon", "Point", "MultiPolygon", "Polygon"], feats
        assert all(isinstance(f["properties"]["_ts"], int) for f in feats)
        assert feats[0]["properties"]["@id"] == "way/1"

        # relation 5: outer stitched from two fragments (one reversed) + hole
        rings = feats[3]["geometry"]["coordinates"]
        assert feats[3]["properties"]["@id"] == "relation/5"
        assert rings[0] == [[20.0, 10.0], [21.0, 10.0], [21.0, 11.0],
                            [20.0, 11.0], [20.0, 10.0]], rings[0]
        assert rings[1][0] == [20.4, 10.4] and rings[1][0] == rings[1][-1], rings[1]
        # its label-role member node emits as a bare Point (osmtogeojson parity)
        assert feats[4]["properties"] == {"@id": "node/99", "_ts": 0}, feats[4]
        assert len(feats[5]["geometry"]["coordinates"]) == 2  # two outers
        assert feats[6]["properties"]["@id"] == "relation/7"  # boundary type

        assert s["schemaVersion"] == 1
        assert s["recencyBandsDays"] == [90, 365, 730]
        assert s["features"] == 7 and s["ways"] == 2 and s["nodes"] == 2
        assert s["relations"] == 3
        # skipped relations: no type tag (4), route (8), unclosed outer (9)
        assert s["relationsSkipped"] == 3 and s["skipped"] == 0
        assert s["editorsCount"] == 2 and s["changesetsCount"] == 3
        assert s["elementVersionsCount"] == 8
        assert s["oldestElement"].startswith("2019-01-01")
        assert s["bbox"] == [-1.0, -1.0, 24.5, 14.5], s["bbox"]
        assert sum(s["editRecencyBands"]) == 4
        assert sum(s["mapperRecencyBands"]) == 2
        assert s["editRecencyBands"][3] == 1
        assert s["qualityMetrics"]["staleElementsCount"] == 1
        assert s["tagCounts"][0] == {"key": "type", "count": 3}, s["tagCounts"]

        # filterDimensions: ";"-split, case-folded counts with dominant-casing
        # display (tie -> first seen: "Asphalt"), desc by count, missing per
        # key, zero-value keys kept (keepEmpty semantics)
        dims = s["filterDimensions"]
        assert dims[0] == {"key": "surface", "kind": "tag",
                           "values": [{"value": "Asphalt", "count": 2},
                                      {"value": "gravel", "count": 1}],
                           "missing": 5}, dims[0]
        assert dims[1] == {"key": "building", "kind": "tag",
                           "values": [{"value": "yes", "count": 1},
                                      {"value": "no", "count": 1}],
                           "missing": 5}, dims[1]
        assert dims[2] == {"key": "nonexistent", "kind": "tag",
                           "values": [], "missing": 7}, dims[2]

        # geometryMix: counts per rendered geometry type; length/area pinned
        # against @turf/length + @turf/area over the same ndjson (bit-identical
        # at time of writing, tolerance for float-order drift only)
        mix = s["geometryMix"]
        assert (mix["points"], mix["lines"], mix["areas"]) == (2, 1, 4), mix
        assert abs(mix["lineKm"] - 157.17776891752186) < 1e-6, mix
        assert abs(mix["areaKm2"] - 22365.44322391079) < 1e-3, mix
        # tag-first area decision (osmtogeojson parity), every ruleset branch
        assert is_area({"building": "yes"})            # polygon: all
        assert not is_area({"highway": "cycleway"})    # not in the highway whitelist
        assert is_area({"highway": "services"})        # in it
        assert is_area({"highway": "cycleway", "area": "yes"})   # area is polygon: all
        assert not is_area({"building": "yes", "area": "no"})    # area=no wins
        assert not is_area({"building": "no"})         # explicitly un-set
        assert not is_area({"natural": "coastline"})   # natural blacklist
        assert is_area({"natural": "water"})           # not blacklisted
        assert is_area({"barrier": "hedge"})
        assert not is_area({"barrier": "fence"})
        assert not is_area({"aeroway": "taxiway"})     # 1-value blacklist, not chars
        # the val == "no" skip is universal, not just for polygon:all keys --
        # osmtogeojson index.js:1001 sits above the mode dispatch at 1004/1006/
        # 1008. Verified against the library: natural=no and aeroway=no both come
        # back LineString, natural=water Polygon. Do not "fix" these to True.
        assert not is_area({"natural": "no"})
        assert not is_area({"aeroway": "no"})
        assert is_area({"aeroway": "apron"})           # blacklist admits everything else
        assert not is_area({})
        assert not is_area({"name": "nowhere"})        # unknown key

        # and that convert() actually consults it: three closed rings, one shape
        src2, dst2 = os.path.join(d, "areas.json"), os.path.join(d, "areas.ndjson")
        ring = ('[{"lat":0,"lon":0},{"lat":0,"lon":1},'
                '{"lat":1,"lon":1},{"lat":0,"lon":0}]')
        with open(src2, "w", encoding="utf-8") as f:
            f.write('{\n  "elements": [\n' + ",\n".join(
                '{"type":"way","id":%d,"geometry":%s,"tags":%s}' % (i, ring, t)
                for i, t in enumerate((
                    '{"building":"yes"}',
                    '{"highway":"cycleway"}',
                    '{"highway":"cycleway","area":"yes"}'))) + "\n  ]\n}\n")
        convert(src2, dst2)
        with open(dst2, encoding="utf-8") as f:
            kinds = [json.loads(line)["geometry"]["type"] for line in f]
        assert kinds == ["Polygon", "LineString", "Polygon"], kinds

        # --json-progress emits one object per tick; anything else is error text
        assert PROGRESS_RE.match('{"progress":33.5}').group(1) == "33.5"
        assert PROGRESS_RE.match("Did not read any valid geometries") is None

        assert apply_settings("[out:json][timeout:9];way;out;", 512, 60) == \
            "[out:json][maxsize:512][timeout:60];way;out;"
        assert apply_settings("way;out;", None, 60) == "[out:json][timeout:60];way;out;"
        assert apply_settings("[out:json];way;", None, None) == "[out:json];way;"
        assert html_error(
            "<html><p>copyright boilerplate</p>"
            "<p><b>Error</b>: line 1: Unknown type &quot;this&quot;</p></html>"
        ) == 'Error : line 1: Unknown type "this"'

        # abort seam: a stale run must strand on its next write, both when the
        # dir is gone and when a re-POST recreated it (generation mismatch)
        global SPOOL
        old_spool, SPOOL = SPOOL, os.path.join(d, "spool")
        try:
            job = {"id": "j1", "state": "queued", "startedAt": now_iso()}
            os.makedirs(jobdir("j1"))
            write_job(job)
            shutil.rmtree(jobdir("j1"))
            try:
                write_job(job)
                raise AssertionError("deleted dir must abort")
            except Aborted:
                pass
            os.makedirs(jobdir("j1"))
            write_job({"id": "j1", "state": "queued", "startedAt": now_iso()})
            try:
                write_job(job)
                raise AssertionError("recreated id must abort the stale run")
            except Aborted:
                pass

            # progress_writer: first tick always writes, the throttle
            # suppresses, and a tick on a deleted job is an abort checkpoint
            job2 = {"id": "j2", "state": "fetching", "startedAt": now_iso()}
            os.makedirs(jobdir("j2"))
            write_job(job2)
            tick = progress_writer(job2, "fetching", interval=3600)
            tick(bytes=1)
            assert read_job("j2")["progress"] == {"stage": "fetching",
                                                  "bytes": 1}
            tick(bytes=2)  # throttled: on-disk progress unchanged
            assert read_job("j2")["progress"]["bytes"] == 1
            shutil.rmtree(jobdir("j2"))
            try:
                progress_writer(job2, "fetching", interval=0)(bytes=3)
                raise AssertionError("tick on a deleted job must abort")
            except Aborted:
                pass

            # sweep: age is last-write mtime; stale dirs go, fresh ones stay
            os.makedirs(jobdir("stale"))
            os.makedirs(jobdir("fresh"))
            old_t = time.time() - (MAX_AGE_DAYS + 1) * DAY
            os.utime(jobdir("stale"), (old_t, old_t))
            sweep()
            assert not os.path.isdir(jobdir("stale"))
            assert os.path.isdir(jobdir("fresh"))

            # recover: running states requeue FIFO with stale progress
            # cleared; a corrupt record is skipped (and logged), not fatal
            os.makedirs(jobdir("bad"))
            with open(os.path.join(jobdir("bad"), "job.json"), "w") as f:
                f.write("not json")
            for jid, state, ts in (("r2", "baking", "2001-02"),
                                   ("r1", "fetching", "2001-01")):
                os.makedirs(jobdir(jid))
                write_job({"id": jid, "state": state, "startedAt": ts,
                           "progress": {"stage": state, "pct": 80}})
            recover()
            order = [Q.get_nowait() for _ in range(3)]
            assert order == ["r1", "r2", "j1"], order  # j1: still-queued above
            assert Q.empty()
            r1 = read_job("r1")
            assert r1["state"] == "queued" and "progress" not in r1, r1
        finally:
            SPOOL = old_spool

        # validate_pmtiles: garbage must not pass as an archive
        bad = os.path.join(d, "bad.pmtiles")
        with open(bad, "wb") as f:
            f.write(b"nope")
        try:
            validate_pmtiles(bad)
            raise AssertionError("garbage archive must fail validation")
        except StageError:
            pass

        # tail_remark: a top-level remark (2-space indent) is a runtime
        # error; the OSM tag remark=* on an element is data, not an error
        rp = os.path.join(d, "tail.json")
        with open(rp, "w", encoding="utf-8") as f:
            f.write('{\n  "elements": [\n{\n  "type": "node",\n'
                    '  "tags": {\n    "remark": "corner note"\n  }\n}\n  ],\n'
                    '  "remark": "runtime error: Query timed out"\n}\n')
        assert tail_remark(rp) == "runtime error: Query timed out"
        with open(rp, "w", encoding="utf-8") as f:
            f.write('{\n  "elements": [\n{\n  "type": "node",\n'
                    '  "tags": {\n    "remark": "corner note"\n  }\n}\n  ]\n}\n')
        assert tail_remark(rp) is None
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        serve()
