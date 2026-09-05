"""Throwaway probe: relations are few, but how much AREA do they carry?

Count understates the loss -- a multipolygon park is one element but can be
larger than every simple-way park combined.

Method and its limit: area comes from a shoelace over each member way, so it is
only sound when a relation's outer members are ALREADY closed rings. Where an
outer ring is fragmented across several members, each fragment's shoelace is
meaningless and the relation's area is undercounted. Properly assembling those
rings is issue #6 -- the thing this probe exists to size -- so instead of
assembling, the last column reports how many relations were fragmented. Read the
area numbers as a floor, and trust them in proportion to that column being low.

Rough equal-area approximation (lon/lat scaled by cos(lat)); only ratios matter.
"""
import json
import math
import ssl
import urllib.request

URL = "https://localhost:8080/api/interpreter"
CTX = ssl._create_unverified_context()

CASES = [
    ("Delft parks", 324269, '["leisure"="park"]'),
    ("Amsterdam parks", 271110, '["leisure"="park"]'),
    ("Amsterdam landuse", 271110, '["landuse"]'),
]

R = 6371000.0


def ring_area(geom):
    """Shoelace area in m^2, lon/lat degrees, cos-latitude corrected.

    Treats the point list as closed. On an open way that is the area enclosed
    by joining last point back to first -- see the module docstring.
    """
    if len(geom) < 3:
        return 0.0
    lat0 = math.radians(sum(p["lat"] for p in geom) / len(geom))
    k = math.pi / 180.0 * R
    pts = [(p["lon"] * k * math.cos(lat0), p["lat"] * k) for p in geom]
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def closed(geom):
    return len(geom) > 3 and geom[0] == geom[-1]


def fetch(area, filt, kind):
    q = (
        "[out:json][timeout:180];"
        "area(%d)->.a;" % (3600000000 + area)
        + "%s%s(area.a);" % (kind, filt)
        + "out geom;"
    )
    body = urllib.request.urlopen(
        urllib.request.Request(URL, data=q.encode()), context=CTX, timeout=300
    ).read()
    return json.loads(body).get("elements", [])


print(
    "%-20s %12s %12s %9s %12s"
    % ("case", "way km2", "relation km2", "rel share", "fragmented")
)
for label, area, filt in CASES:
    wa = sum(ring_area(e.get("geometry") or []) for e in fetch(area, filt, "way"))
    ra = 0.0
    rels = fetch(area, filt, "relation")
    frag = 0
    for e in rels:
        outers = []
        for m in e.get("members") or []:
            # outer rings add, inner rings (holes) subtract
            g = m.get("geometry") or []
            ra += ring_area(g) * (-1.0 if m.get("role") == "inner" else 1.0)
            if m.get("type") == "way" and m.get("role") != "inner":
                outers.append(g)
        if outers and not all(closed(g) for g in outers):
            frag += 1
    tot = wa + ra
    share = (100.0 * ra / tot) if tot else 0.0
    print(
        "%-20s %12.3f %12.3f %8.1f%% %7d/%-4d"
        % (label, wa / 1e6, ra / 1e6, share, frag, len(rels))
    )
