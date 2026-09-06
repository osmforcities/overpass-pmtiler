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

import config
from config import (HERE, ID_RE, MAX_QUERY, MAX_MAXSIZE, MAX_TIMEOUT,
                    RUNNING, OUTPUTS, DAY, BANDS, EARTH_R, MAX_FILTER_KEYS,
                    log, now_iso)


from errors import Aborted, StageError
from spool import (jobdir, progress_writer, read_job, set_state, sweep,
                   write_job)
from overpass import apply_settings, html_error, tail_remark
from convert import brace_delta, convert, is_area
from bake import PROGRESS_RE, validate_pmtiles
from runner import Q, recover, worker

CREATE_LOCK = threading.Lock()


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
                 for j in filter(None, map(read_job, os.listdir(config.SPOOL)))),
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
    os.makedirs(config.SPOOL, exist_ok=True)
    sweep()
    recover()
    threading.Thread(target=worker, daemon=True).start()
    httpd = Server((config.BIND, config.PORT), Handler)
    log("pmtiler on http://%s:%d spool=%s overpass=%s"
        % (config.BIND, httpd.server_address[1], config.SPOOL, config.OVERPASS))
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

        # string-aware brace counting: braces in tag values are data
        assert brace_delta(b'    "note": "curly { brace",') == 0
        assert brace_delta(b'  "tags": {') == 1
        assert brace_delta(b'  "a\\"{": {') == 1  # escaped quote stays inside
        assert brace_delta(b'},') == -1

        # an unbalanced "{" inside a tag value must not desync the splitter
        # (before brace_delta it swallowed every following element and charged
        # the whole loss as skipped=1)
        braced = os.path.join(d, "braces.json")
        with open(braced, "w", encoding="utf-8") as f:
            f.write('{\n  "elements": [\n{\n  "type": "node",\n  "id": 1,\n'
                    '  "lat": 1.0,\n  "lon": 1.0,\n  "tags": {\n'
                    '    "note": "curly { brace"\n  }\n},\n{\n'
                    '  "type": "node",\n  "id": 2,\n  "lat": 2.0,\n'
                    '  "lon": 2.0\n}\n  ]\n}\n')
        bs = convert(braced, os.path.join(d, "braces.ndjson"))
        assert bs["features"] == 2 and bs["skipped"] == 0, bs

        # non-pretty-printed input fails loudly at the convert stage
        compact = os.path.join(d, "compact.json")
        with open(compact, "w", encoding="utf-8") as f:
            f.write('{"version":0.6,"elements":[{"type":"node","id":1,'
                    '"lat":1.0,"lon":1.0}]}')
        try:
            convert(compact, os.path.join(d, "compact.ndjson"))
            raise AssertionError("compact overpass output must fail loudly")
        except StageError as e:
            assert "overpass response format" in str(e), e

        # abort seam: a stale run must strand on its next write, both when the
        # dir is gone and when a re-POST recreated it (generation mismatch)
        old_spool, config.SPOOL = config.SPOOL, os.path.join(d, "spool")
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
            old_t = time.time() - (config.MAX_AGE_DAYS + 1) * DAY
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
            config.SPOOL = old_spool

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
