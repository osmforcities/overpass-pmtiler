"""The convert stage: stream Overpass JSON to nd-geojson plus stats.

One bounded-memory pass. Conversion is defined as osmtogeojson-0.9.2
-compatible and measures as @turf-compatible spherical geodesy -- see the
README "Compatibility targets" section.
"""
import datetime
import json
import math
import os
import resource
import time

from config import BANDS, DAY, EARTH_R
from errors import StageError

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


def brace_delta(line):
    """Net {/} count outside JSON strings for one line. A JSON string cannot
    hold a literal newline, so every line starts and ends outside a string --
    per-line scanning needs no carried state. A naive count() breaks on a
    brace inside a tag value and silently desyncs the element splitter."""
    if b"{" not in line and b"}" not in line:  # fast path: most lines
        return 0
    depth = 0
    in_string = escaped = False
    for byte in line:
        if escaped:
            escaped = False
        elif byte == 0x5C:      # backslash
            escaped = in_string
        elif byte == 0x22:      # quote
            in_string = not in_string
        elif not in_string:
            if byte == 0x7B:    # {
                depth += 1
            elif byte == 0x7D:  # }
                depth -= 1
    return depth


def convert(inp, outp, tick=None, filter_keys=()):
    """Overpass JSON -> nd-geojson + accumulator stats in one bounded-memory pass.

    Splits the elements array on element boundaries by string-aware brace
    depth (line-based; Overpass pretty-prints -- anything else fails loudly
    below), then json.loads per element: 13 MB RSS at 2.23 GB in.
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
        found = False
        for line in f:  # skip the header, stop at the elements array
            read += len(line)
            if line.startswith(b'  "elements":'):
                found = True
                break
        if not found:
            # compact or foreign JSON would otherwise convert to 0 features
            # and surface as a tippecanoe error two stages downstream
            raise StageError("unexpected overpass response format "
                             "(expected pretty-printed [out:json] output)")
        depth = 0
        buf = []
        for line in f:
            read += len(line)
            s = line.strip()
            if depth == 0:
                if s.startswith(b"{"):
                    depth = brace_delta(s)
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
                depth += brace_delta(s)
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
