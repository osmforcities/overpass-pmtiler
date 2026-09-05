"""Throwaway probe: how much of a template's result is relations?

Counts node/way/relation separately for a few real template shapes, using the
app's own query form (node+way+relation per OR group). Read-only.
"""
import json
import ssl
import sys
import urllib.request

URL = "https://localhost:8080/api/interpreter"
CTX = ssl._create_unverified_context()

# (label, area relation id, tag filter) -- the app emits all three element types
CASES = [
    ("Delft buildings", 324269, '["building"]'),
    ("Delft parks", 324269, '["leisure"="park"]'),
    ("Delft cycleways", 324269, '["highway"="cycleway"]'),
    ("Delft drinking water", 324269, '["amenity"="drinking_water"]'),
    ("Amsterdam buildings", 271110, '["building"]'),
    ("Amsterdam parks", 271110, '["leisure"="park"]'),
    ("Amsterdam landuse", 271110, '["landuse"]'),
]


def count(area, filt, kind):
    q = (
        "[out:json][timeout:120];"
        "area(%d)->.a;" % (3600000000 + area)
        + "%s%s(area.a);" % (kind, filt)
        + "out count;"
    )
    body = urllib.request.urlopen(
        urllib.request.Request(URL, data=q.encode()), context=CTX, timeout=180
    ).read()
    data = json.loads(body)
    els = data.get("elements") or [{}]
    return int(els[0].get("tags", {}).get("total", 0))


print("%-24s %9s %9s %9s %8s" % ("case", "nodes", "ways", "relations", "rel %"))
for label, area, filt in CASES:
    try:
        n = count(area, filt, "node")
        w = count(area, filt, "way")
        r = count(area, filt, "relation")
    except Exception as exc:  # probe -- keep going on a failing case
        print("%-24s FAILED: %s" % (label, exc))
        continue
    total = n + w + r
    pct = (100.0 * r / total) if total else 0.0
    print("%-24s %9d %9d %9d %7.2f%%" % (label, n, w, r, pct))
    sys.stdout.flush()
