"""Self-check for the converter and the spool machinery, plus fetch
transport failures against loopback sockets only and bake runs against
tippecanoe and tile-join shims -- no external network, never the real tools. Run via
`python3 pmtiler.py --selftest`. Plain asserts, one fixture, no framework."""
import json
import math
import os
import re
import http.server
import shutil
import socket
import threading
import time

import config
import overpass
from bake import PROGRESS_RE, validate_pmtiles
from config import DAY, now_iso
from convert import (OVERLAP_CELL_SIZES_M, brace_delta, cell_overlap, convert,
                     is_area, local_density_per_km2)
from errors import Aborted, StageError
from overpass import (apply_settings, classify_error, head_error,
                      html_error, tail_remark)
from runner import Q, recover, run_job
from spool import (create_job, job_directory, list_jobs, progress_writer,
                   read_job, sweep, write_job)


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

# The page Overpass serves instead of data when it refuses a query, shaped like
# the real one (an areas-dispatcher refusal). check.py's stub serves it as
# pmtiler.ERROR_PAGE, so both checks exercise one body.
ERROR_PAGE = ('<?xml version="1.0" encoding="UTF-8"?>\n<html><body>'
              '<p>copyright boilerplate</p>'
              '<p><strong style="color:#FF0000">Error</strong>: runtime error:'
              ' open64: 0 Success /osm3s_areas Dispatcher_Client'
              '::request_read_and_idx::protocol_error</p>'
              '</body></html>\n')


def selftest():
    scratch_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               ".selftest")
    os.makedirs(scratch_dir, exist_ok=True)
    try:
        source_path = os.path.join(scratch_dir, "raw.json")
        output_path = os.path.join(scratch_dir, "out.ndjson")
        with open(source_path, "w", encoding="utf-8") as f:
            f.write(FIXTURE)
        ticks = []
        stats = convert(source_path, output_path,
                        lambda **kw: ticks.append(kw),  # unthrottled
                        filter_keys=["surface", "building", "nonexistent"])
        with open(output_path, encoding="utf-8") as f:
            features = [json.loads(line) for line in f]

        # convert knows its denominator: the input file is complete by now
        # (relation 5 ticks once but adds two features: itself + member node)
        assert len(ticks) == 6, ticks
        assert [t["features"] for t in ticks] == [1, 2, 3, 5, 6, 7], ticks
        assert all(t["total"] == os.path.getsize(source_path)
                   for t in ticks), ticks
        assert [t["bytes"] for t in ticks] == sorted(t["bytes"] for t in ticks)
        # < 100 at the last feature: the skipped relations and the closing
        # brackets are still ahead of the read cursor
        assert 60 < ticks[-1]["pct"] < 100, ticks[-1]

        assert [f["geometry"]["type"] for f in features] == \
            ["Polygon", "LineString", "Point",
             "Polygon", "Point", "MultiPolygon", "Polygon"], features
        assert all(isinstance(f["properties"]["_ts"], int) for f in features)
        assert features[0]["properties"]["@id"] == "way/1"

        # relation 5: outer stitched from two fragments (one reversed) + hole
        rings = features[3]["geometry"]["coordinates"]
        assert features[3]["properties"]["@id"] == "relation/5"
        assert rings[0] == [[20.0, 10.0], [21.0, 10.0], [21.0, 11.0],
                            [20.0, 11.0], [20.0, 10.0]], rings[0]
        assert rings[1][0] == [20.4, 10.4] and rings[1][0] == rings[1][-1], rings[1]
        # its label-role member node emits as a bare Point (osmtogeojson parity)
        assert features[4]["properties"] == {"@id": "node/99", "_ts": 0}, features[4]
        assert len(features[5]["geometry"]["coordinates"]) == 2  # two outers
        assert features[6]["properties"]["@id"] == "relation/7"  # boundary type

        assert stats["schemaVersion"] == 1
        assert stats["recencyBandsDays"] == [90, 365, 730]
        assert stats["features"] == 7 and stats["ways"] == 2 and stats["nodes"] == 2
        assert stats["relations"] == 3
        # skipped relations: no type tag (4), route (8), unclosed outer (9)
        assert stats["relationsSkipped"] == 3 and stats["skipped"] == 0
        assert stats["editorsCount"] == 2 and stats["changesetsCount"] == 3
        assert stats["elementVersionsCount"] == 8
        assert stats["oldestElement"].startswith("2019-01-01")
        assert stats["bbox"] == [-1.0, -1.0, 24.5, 14.5], stats["bbox"]
        assert sum(stats["editRecencyBands"]) == 4
        assert sum(stats["mapperRecencyBands"]) == 2
        assert stats["editRecencyBands"][3] == 1
        assert stats["qualityMetrics"]["staleElementsCount"] == 1
        assert stats["tagCounts"][0] == {"key": "type", "count": 3}, stats["tagCounts"]

        # filterDimensions: ";"-split, case-folded counts with dominant-casing
        # display (tie -> first seen: "Asphalt"), desc by count, missing per
        # key, zero-value keys kept (keepEmpty semantics)
        dimensions = stats["filterDimensions"]
        assert dimensions[0] == {"key": "surface", "kind": "tag",
                           "values": [{"value": "Asphalt", "count": 2},
                                      {"value": "gravel", "count": 1}],
                           "missing": 5}, dimensions[0]
        assert dimensions[1] == {"key": "building", "kind": "tag",
                           "values": [{"value": "yes", "count": 1},
                                      {"value": "no", "count": 1}],
                           "missing": 5}, dimensions[1]
        assert dimensions[2] == {"key": "nonexistent", "kind": "tag",
                           "values": [], "missing": 7}, dimensions[2]

        # geometryMix: counts per rendered geometry type; length/area pinned
        # against @turf/length + @turf/area over the same ndjson (bit-identical
        # at time of writing, tolerance for float-order drift only)
        mix = stats["geometryMix"]
        assert (mix["points"], mix["lines"], mix["areas"]) == (2, 1, 4), mix
        assert abs(mix["lineKm"] - 157.17776891752186) < 1e-6, mix
        assert abs(mix["areaKm2"] - 22365.44322391079) < 1e-3, mix
        # the node and the member node: one per cell, so 1 / cell area
        assert abs(mix["pointsPerKm2"] - 1 / (0.005 * 110.57 * 0.005 * 111.32
                   * math.cos(math.radians((-1.0 + 10.5) / 2)))) < 0.005, mix
        assert mix["areasPerKm2"] > 0, mix
        assert [size for size, _ in mix["cellOverlap"]] == OVERLAP_CELL_SIZES_M, mix

        # local density: a town of 100 points in one ~500 m cell plus three
        # far outliers reads as dense, where a count over the bbox would not
        town = [coordinate for i in range(100)
                for coordinate in (4.9001 + i * 0.00003, 52.3701)]
        outliers = [5.9, 53.3, 3.9, 51.3, 6.5, 52.0]
        cell_km2 = 0.005 * 110.57 * 0.005 * 111.32 * math.cos(
            math.radians((52.3701 * 100 + 53.3 + 51.3 + 52.0) / 103))
        assert abs(local_density_per_km2(town + outliers)
                   - (100 * 100 + 3) / 103 / cell_km2) < 1e-6
        assert local_density_per_km2(town + outliers) > 75
        assert local_density_per_km2([]) == 0
        # cell overlap: two features metres apart share every cell, a far
        # one never does, so a third of the features overlap at every size
        overlap = cell_overlap([0.00001, 0.00001, 0.00002, 0.00002, 1.0, 1.0])
        assert [size for size, _ in overlap] == OVERLAP_CELL_SIZES_M, overlap
        assert all(abs(share - 1 / 3) < 1e-4 for _, share in overlap), overlap
        # about 100 m apart: one cell from 128 m up, two below
        assert cell_overlap([0.00001, 0.00001, 0.0009, 0.00001]) == [
            [1024, 0.5], [512, 0.5], [256, 0.5], [128, 0.5],
            [64, 0.0], [32, 0.0], [16, 0.0]]
        assert cell_overlap([]) == []
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

        # keepMeta + ageBandsDays: opt-in per job, default output unchanged
        assert "@user" not in features[0]["properties"], features[0]
        assert "ageBands" not in stats
        meta_output_path = os.path.join(scratch_dir, "meta.ndjson")
        meta_stats = convert(source_path, meta_output_path,
                             keep_meta=True, age_bands_days=[7, 30, 90])
        with open(meta_output_path, encoding="utf-8") as f:
            meta_features = [json.loads(line) for line in f]
        first = meta_features[0]["properties"]
        assert first["@user"] == "ada" and first["@version"] == 3, first
        assert first["@changeset"] == 11, first
        assert first["@timestamp"] == "2026-08-01T00:00:00Z", first
        assert first["_ts"] and first["@id"] == "way/1", first
        # member node stays bare: synthetic identity only, no meta to keep
        assert meta_features[4]["properties"] == {"@id": "node/99", "_ts": 0}
        # per-FEATURE counts, cutoffs+1 buckets; the three undated features
        # (two skipless relations without timestamps + the member node) land
        # in the final bucket alongside anything older than the last cutoff
        bands = meta_stats["ageBands"]
        assert len(bands) == 4 and sum(bands) == meta_stats["features"], bands
        assert bands[-1] >= 3, bands
        # ageBands is additive: the rest of the stats are unchanged
        assert meta_stats["editRecencyBands"] == stats["editRecencyBands"]

        # and that convert() actually consults it: three closed rings, one shape
        area_source_path = os.path.join(scratch_dir, "areas.json")
        area_output_path = os.path.join(scratch_dir, "areas.ndjson")
        ring = ('[{"lat":0,"lon":0},{"lat":0,"lon":1},'
                '{"lat":1,"lon":1},{"lat":0,"lon":0}]')
        with open(area_source_path, "w", encoding="utf-8") as f:
            f.write('{\n  "elements": [\n' + ",\n".join(
                '{"type":"way","id":%d,"geometry":%s,"tags":%s}' % (i, ring, t)
                for i, t in enumerate((
                    '{"building":"yes"}',
                    '{"highway":"cycleway"}',
                    '{"highway":"cycleway","area":"yes"}'))) + "\n  ]\n}\n")
        convert(area_source_path, area_output_path)
        with open(area_output_path, encoding="utf-8") as f:
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

        # over-ceiling failures classify as too_large; anything else stays
        # unclassified (retryable by the caller)
        assert classify_error(
            "runtime error: Query run out of memory using about 2048 MB "
            "of RAM.") == "too_large"
        assert classify_error(
            "runtime error: open64: 0 Success /osm3s_areas "
            "Dispatcher_Client::request_read_and_idx::timeout") == "too_large"
        assert classify_error("Error: line 1: parse error") is None
        assert classify_error("overpass read timed out after 240s") is None

        # string-aware brace counting: braces in tag values are data
        assert brace_delta(b'    "note": "curly { brace",') == 0
        assert brace_delta(b'  "tags": {') == 1
        assert brace_delta(b'  "a\\"{": {') == 1  # escaped quote stays inside
        assert brace_delta(b'},') == -1

        # an unbalanced "{" inside a tag value must not desync the splitter
        # (before brace_delta it swallowed every following element and charged
        # the whole loss as skipped=1)
        braced = os.path.join(scratch_dir, "braces.json")
        with open(braced, "w", encoding="utf-8") as f:
            f.write('{\n  "elements": [\n{\n  "type": "node",\n  "id": 1,\n'
                    '  "lat": 1.0,\n  "lon": 1.0,\n  "tags": {\n'
                    '    "note": "curly { brace"\n  }\n},\n{\n'
                    '  "type": "node",\n  "id": 2,\n  "lat": 2.0,\n'
                    '  "lon": 2.0\n}\n  ]\n}\n')
        braced_stats = convert(braced, os.path.join(scratch_dir, "braces.ndjson"))
        assert braced_stats["features"] == 2 \
            and braced_stats["skipped"] == 0, braced_stats

        # malformed or unexpected elements are counted and skipped, never
        # crash the conversion, and the valid elements around them survive
        hostile = os.path.join(scratch_dir, "hostile.json")
        with open(hostile, "w", encoding="utf-8") as f:
            f.write('{\n  "elements": [\n' + ",\n".join((
                '{"type":"node","id":1,"lat":}',                  # bad JSON
                '{"type":"area","id":2}',                         # unknown type
                '{"type":"node","id":3,"lon":1.0}',               # no lat
                '{"type":"relation","id":4,"members":['           # bad member
                '{"type":"way","role":"outer","geometry":[{"lat":0}]}],'
                '"tags":{"type":"multipolygon"}}',
                '{"type":"relation","id":5,"members":['
                '{"type":"way","ref":50,"role":"inner"},'  # no geometry
                '{"type":"way","ref":51,"role":"outer","geometry":%s}],'
                '"tags":{"type":"multipolygon"}}' % ring,
                '{"type":"node","id":6,"lat":2.0,"lon":2.0}',
            )) + "\n  ]\n}\n")
        hostile_output = os.path.join(scratch_dir, "hostile.ndjson")
        hostile_stats = convert(hostile, hostile_output)
        assert hostile_stats["skipped"] == 3, hostile_stats
        assert hostile_stats["relationsSkipped"] == 1, hostile_stats
        with open(hostile_output, encoding="utf-8") as f:
            survivors = [json.loads(line) for line in f]
        assert [(s["properties"]["@id"], s["geometry"]["type"])
                for s in survivors] == [("relation/5", "Polygon"),
                                        ("node/6", "Point")], survivors

        # non-pretty-printed input fails loudly at the convert stage
        compact = os.path.join(scratch_dir, "compact.json")
        with open(compact, "w", encoding="utf-8") as f:
            f.write('{"version":0.6,"elements":[{"type":"node","id":1,'
                    '"lat":1.0,"lon":1.0}]}')
        try:
            convert(compact, os.path.join(scratch_dir, "compact.ndjson"))
            raise AssertionError("compact overpass output must fail loudly")
        except StageError as e:
            assert "overpass response format" in str(e), e

        # abort seam: a stale run must strand on its next write when the dir
        # is gone, when a re-POST recreated it (generation mismatch), and when
        # a DELETE left the dir behind without job.json
        old_spool, config.SPOOL = config.SPOOL, os.path.join(scratch_dir, "spool")
        try:
            job = {"id": "j1", "state": "queued", "startedAt": now_iso()}
            create_job(job)
            shutil.rmtree(job_directory("j1"))
            try:
                write_job(job)
                raise AssertionError("deleted dir must abort")
            except Aborted:
                pass
            create_job({"id": "j1", "state": "queued", "startedAt": now_iso()})
            try:
                write_job(job)
                raise AssertionError("recreated id must abort the stale run")
            except Aborted:
                pass
            orphan = {"id": "orphan", "state": "baking", "startedAt": now_iso()}
            create_job(orphan)
            os.remove(os.path.join(job_directory("orphan"), "job.json"))
            try:
                write_job(orphan)
                raise AssertionError("record-less dir must abort")
            except Aborted:
                pass
            assert read_job("orphan") is None, "aborted write recreated the record"

            # progress_writer: first tick always writes, the throttle
            # suppresses, and a tick on a deleted job is an abort checkpoint
            progress_job = {"id": "j2", "state": "fetching", "startedAt": now_iso()}
            create_job(progress_job)
            tick = progress_writer(progress_job, "fetching", interval=3600)
            tick(bytes=1)
            assert read_job("j2")["progress"] == {"stage": "fetching",
                                                  "bytes": 1}
            tick(bytes=2)  # throttled: on-disk progress unchanged
            assert read_job("j2")["progress"]["bytes"] == 1
            shutil.rmtree(job_directory("j2"))
            try:
                progress_writer(progress_job, "fetching", interval=0)(bytes=3)
                raise AssertionError("tick on a deleted job must abort")
            except Aborted:
                pass

            # sweep: age is last-write mtime; stale dirs go, fresh ones stay
            os.makedirs(job_directory("stale"))
            os.makedirs(job_directory("fresh"))
            stale_mtime = time.time() - (config.MAX_AGE_DAYS + 1) * DAY
            os.utime(job_directory("stale"), (stale_mtime, stale_mtime))
            sweep()
            assert not os.path.isdir(job_directory("stale"))
            assert os.path.isdir(job_directory("fresh"))

            # recover: running states requeue FIFO with stale progress
            # cleared; a corrupt record is skipped (and logged), not fatal
            os.makedirs(job_directory("bad"))
            with open(os.path.join(job_directory("bad"), "job.json"), "w") as f:
                f.write("not json")
            for jid, state, ts in (("r2", "baking", "2001-02"),
                                   ("r1", "fetching", "2001-01")):
                create_job({"id": jid, "state": state, "startedAt": ts,
                            "progress": {"stage": state, "pct": 80}})
            recover()
            order = [Q.get_nowait() for _ in range(3)]
            assert order == ["r1", "r2", "j1"], order  # j1: still-queued above
            assert Q.empty()
            r1 = read_job("r1")
            assert r1["state"] == "queued" and "progress" not in r1, r1

            # list_jobs: readable records only -- the corrupt "bad" and the
            # record-less "fresh" dir are skipped
            ids = sorted(job["id"] for job in list_jobs())
            assert ids == ["j1", "r1", "r2"], ids

            # fetch transport failures end the job failed, unclassified:
            # a just-closed port refuses the connection, a listener that
            # never accepts takes the request into its backlog and never
            # answers (READ_SLACK shrunk so the wait is 1 s, not 240), and
            # a 429 whose page names Dispatcher_Client must not classify
            old_overpass, old_slack = config.OVERPASS, overpass.READ_SLACK
            with socket.socket() as closed:
                closed.bind(("127.0.0.1", 0))
                closed_port = closed.getsockname()[1]
            stalled = socket.socket()

            class RateLimited(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    self.send_error(429, explain=(
                        "Error: runtime error: open64: 0 Success"
                        " /osm3s_osm_base Dispatcher_Client::"
                        "request_read_and_idx::rate_limited"))

                def log_message(self, *a):
                    pass

            limiter = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      RateLimited)
            threading.Thread(target=limiter.serve_forever, daemon=True).start()
            try:
                stalled.bind(("127.0.0.1", 0))
                stalled.listen()
                overpass.READ_SLACK = 0
                for jid, port, prefix, detail in (
                        ("down", closed_port, "overpass unreachable: ",
                         "refused"),
                        ("stall", stalled.getsockname()[1],
                         "overpass read timed out after 1s", ""),
                        ("limited", limiter.server_address[1], "Error",
                         "rate_limited")):
                    config.OVERPASS = \
                        "http://127.0.0.1:%d/api/interpreter" % port
                    create_job({"id": jid, "query": "way;out;", "timeout": 1,
                                "state": "queued", "startedAt": now_iso()})
                    run_job(jid)
                    failed = read_job(jid)
                    assert failed["state"] == "failed", failed
                    assert failed["error"].startswith(prefix), failed
                    assert detail in failed["error"], failed
                    assert "errorKind" not in failed, failed
            finally:
                stalled.close()
                limiter.shutdown()
                limiter.server_close()
                config.OVERPASS, overpass.READ_SLACK = old_overpass, old_slack

            # a failing tippecanoe ends the job failed with its last 20 stderr
            # lines capped at 2000 chars, progress ticks left out: a shim
            # earlier on PATH stands in
            shim_dir = os.path.join(scratch_dir, "bin")
            os.makedirs(shim_dir)
            shim_path = os.path.join(shim_dir, "tippecanoe")

            user_agents = []

            class FixtureOverpass(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    user_agents.append(self.headers["User-Agent"])
                    # an unread body makes the close a TCP reset
                    self.rfile.read(int(self.headers["Content-Length"]))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(FIXTURE.encode())

                def log_message(self, *a):
                    pass

            data_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                          FixtureOverpass)
            threading.Thread(target=data_server.serve_forever,
                             daemon=True).start()
            old_overpass, old_path = config.OVERPASS, os.environ["PATH"]
            try:
                config.OVERPASS = "http://127.0.0.1:%d/api/interpreter" % \
                    data_server.server_address[1]
                os.environ["PATH"] = shim_dir + os.pathsep + old_path
                errors = {}
                for jid, stderr in (
                        ("bake", 'for i in $(seq 1 25); do '
                                 'echo "bad input $i" >&2; done\n'),
                        ("bake-long", 'printf "head %03000d tail\\n" 0 >&2\n')):
                    # progress last: inside the 20-line tail, so only the
                    # filter keeps it out of the error
                    with open(shim_path, "w") as f:
                        f.write('#!/bin/sh\n' + stderr
                                + 'echo \'{"progress":12.5}\' >&2\nexit 1\n')
                    os.chmod(shim_path, 0o755)
                    create_job({"id": jid, "query": "way;out;",
                                "state": "queued", "startedAt": now_iso()})
                    run_job(jid)
                    failed = read_job(jid)
                    assert failed["state"] == "failed", failed
                    errors[jid] = failed["error"]
                assert user_agents[0] == config.USER_AGENT, user_agents
                error = errors["bake"]
                assert error.startswith("tippecanoe: bad input 6\n"), error
                assert error.endswith("bad input 25"), error
                assert "progress" not in error, error
                # the cap keeps the end, where tippecanoe's fatal line sits
                assert errors["bake-long"] == \
                    "tippecanoe: " + "0" * 1995 + " tail", \
                    errors["bake-long"][:80]

                # one feature location: -zg refuses, the rebake at a fixed
                # maxzoom lands the job done; other arguments stay identical
                header_path = os.path.join(scratch_dir, "one-tile.pmtiles")
                with open(header_path, "wb") as f:
                    f.write(b"PMTiles\x03" + bytes(64) + b"\x01" + bytes(54))
                calls_path = os.path.join(scratch_dir, "calls")
                # the decode shim answers the tile sizes a test stages per
                # archive name, none staged meaning every tile fits
                decode_path = os.path.join(shim_dir, "tippecanoe-decode")
                sizes_dir = os.path.join(scratch_dir, "sizes")
                os.makedirs(sizes_dir)
                with open(decode_path, "w") as f:
                    f.write('#!/bin/sh\necho "$(basename $0) $*" >> %s\n'
                            'cat %s/$2.json 2>/dev/null || echo "[]"\n'
                            % (calls_path, sizes_dir))
                os.chmod(decode_path, 0o755)
                with open(shim_path, "w") as f:
                    f.write('#!/bin/sh\necho "$*" >> %s\n'
                            'case " $* " in *" -zg "*)\n'
                            '  echo "Can\'t guess maxzoom (-zg) without at least'
                            ' two distinct feature locations" >&2; exit 110;;\n'
                            'esac\n'
                            'while [ "$1" != "-o" ]; do shift; done\n'
                            'cp %s "$2"\n' % (calls_path, header_path))
                create_job({"id": "one-location", "query": "way;out;",
                            "state": "queued", "startedAt": now_iso()})
                run_job("one-location")
                done = read_job("one-location")
                assert done["state"] == "done", done
                with open(calls_path) as f:
                    calls = f.read().splitlines()
                assert len(calls) == 4, calls
                assert calls[1] == calls[0].replace(" -zg ", " -z14 "), calls
                assert calls[3] == "tippecanoe-decode --stats sparse.pmtiles", \
                    calls

                # a full bake reaching z14 gets z9-z13 rebaked with only the
                # drawn keys and z14+ with every tag, joined after its own
                # z0-z8; a full bake stopping at z13 gets every zoom rebaked
                # without a drop rate, the probe itself being the archive
                # when every tile fits
                tile_join_path = os.path.join(shim_dir, "tile-join")
                copy_header_shim = ('#!/bin/sh\necho "$(basename $0) $*" >> %s\n'
                                    'while [ "$1" != "-o" ]; do shift; done\n'
                                    'cp %s "$2"\n' % (calls_path, header_path))
                for path in (shim_path, tile_join_path):
                    with open(path, "w") as f:
                        f.write(copy_header_shim)
                    os.chmod(path, 0o755)

                def stage_sizes(name, sizes):
                    with open(os.path.join(sizes_dir, name + ".json"), "w") as f:
                        json.dump([{"zoom": zoom, "bytes": size}
                                   for zoom, size in sizes.items()], f)

                def write_header(max_zoom):
                    with open(header_path, "wb") as f:
                        f.write(b"PMTiles\x03" + bytes(64) + b"\x01"
                                + bytes(28) + bytes([max_zoom]) + bytes(25))

                for max_zoom, jid in ((14, "reaches-z14"), (13, "stops-z13")):
                    write_header(max_zoom)
                    open(calls_path, "w").close()
                    create_job({"id": jid, "query": "way;out;",
                                "filterableTags": ["building"],
                                "state": "queued", "startedAt": now_iso()})
                    run_job(jid)
                    done = read_job(jid)
                    assert done["state"] == "done", done
                    with open(calls_path) as f:
                        calls = f.read().splitlines()
                    assert calls[0].startswith("tippecanoe -f -zg "), calls
                    if max_zoom == 13:
                        sparse = calls[1]
                        assert " -Z0 -z13 -B14 -r1.0000 " in sparse, sparse
                        assert " -y " not in sparse, sparse
                        assert " --no-tile-size-limit " in sparse, sparse
                        assert calls[2:] == [
                            "tippecanoe-decode --stats sparse.pmtiles"], calls
                        assert sorted(f for f in os.listdir(job_directory(jid))
                                      if f.endswith(".pmtiles")) == \
                            ["output.pmtiles"], os.listdir(job_directory(jid))
                        continue
                    high = calls[1]
                    assert " -Z14 -z14 -B15 -r1.0000 " in high, high
                    assert " -y " not in high, high
                    assert calls[2] == "tippecanoe-decode --stats high.pmtiles"
                    slim = calls[3]
                    assert " -Z9 -z13 -B14 -r1.0000 " in slim, slim
                    assert " -y building -y _ts -y @id " in slim, slim
                    assert " --no-tile-size-limit " in slim, slim
                    assert slim.endswith(" -o %s %s" % (
                        os.path.join(job_directory(jid), "slim.pmtiles"),
                        os.path.join(job_directory(jid), "data.ndjson"))), slim
                    join = "tile-join -f -q --no-tile-size-limit "
                    assert calls[4:] == [
                        "tippecanoe-decode --stats slim.pmtiles",
                        join + "-z8 -o low.pmtiles full.pmtiles",
                        join + "-o output.pmtiles low.pmtiles slim.pmtiles"
                               " high.pmtiles"], calls
                    assert sorted(f for f in os.listdir(job_directory(jid))
                                  if f.endswith(".pmtiles")) == \
                        ["output.pmtiles"], os.listdir(job_directory(jid))

                def bake_dense(jid, staged, max_zoom=14):
                    for name, sizes in staged.items():
                        stage_sizes(name, sizes)
                    write_header(max_zoom)
                    open(calls_path, "w").close()
                    create_job({"id": jid, "query": "way;out;",
                                "state": "queued", "startedAt": now_iso()})
                    run_job(jid)
                    done = read_job(jid)
                    assert done["state"] == "done", done
                    assert sorted(f for f in os.listdir(job_directory(jid))
                                  if f.endswith(".pmtiles")) == \
                        ["output.pmtiles"], os.listdir(job_directory(jid))
                    shutil.rmtree(sizes_dir)
                    os.makedirs(sizes_dir)
                    with open(calls_path) as f:
                        calls = f.read().splitlines()
                    # a per-zoom part is its own bake's max zoom, where
                    # simplification drops small polygons
                    for call in calls:
                        if (call.startswith("tippecanoe ")
                                and re.search(r"-z\d+\.pmtiles ", call)):
                            assert " --no-simplification " in call, call
                    return ([call.split(" --drop-polygons")[0] for call in calls
                             if call.startswith("tippecanoe -f -Z")],
                            [call for call in calls
                             if call.startswith("tile-join")])

                # over-cap z14 gains z15 and is thinned by one rate; no zoom
                # under it keeps a bigger share, so the fitting slim z13 is
                # thinned to z14's rate and the zooms under over-cap z12 to
                # z12's
                bakes, joins = bake_dense("dense", {
                    "slim.pmtiles": {11: 450_000, 12: 2_000_000, 13: 100},
                    "slim-z12.pmtiles": {12: 400_000},
                    "high.pmtiles": {14: 900_000},
                    "high-z14.pmtiles": {14: 450_000}})
                assert bakes == [
                    "tippecanoe -f -Z14 -z14 -B15 -r1.0000",
                    "tippecanoe -f -Z15 -z15 -B16 -r1.0000",
                    "tippecanoe -f -Z14 -z14 -B15 -r2.2500",
                    "tippecanoe -f -Z9 -z13 -B14 -r1.0000",
                    "tippecanoe -f -Z13 -z13 -B14 -r2.2500",
                    "tippecanoe -f -Z12 -z12 -B13 -r5.0000",
                    "tippecanoe -f -Z11 -z11 -B12 -r5.0000",
                    "tippecanoe -f -Z10 -z10 -B11 -r5.0000",
                    "tippecanoe -f -Z9 -z9 -B10 -r5.0000"], bakes
                assert joins == [
                    join + "-z8 -o low.pmtiles full.pmtiles",
                    join + "-o output.pmtiles low.pmtiles slim-z13.pmtiles"
                           " slim-z12.pmtiles slim-z11.pmtiles slim-z10.pmtiles"
                           " slim-z9.pmtiles high-z15.pmtiles"
                           " high-z14.pmtiles"], joins

                # the fitting zooms above the highest thinned one are cut
                # whole from the probe, not rebaked
                bakes, joins = bake_dense("dense-slim", {
                    "slim.pmtiles": {11: 2_000_000, 12: 450_000},
                    "slim-z11.pmtiles": {11: 400_000}})
                assert bakes == [
                    "tippecanoe -f -Z14 -z14 -B15 -r1.0000",
                    "tippecanoe -f -Z9 -z13 -B14 -r1.0000",
                    "tippecanoe -f -Z11 -z11 -B12 -r5.0000",
                    "tippecanoe -f -Z10 -z10 -B11 -r5.0000",
                    "tippecanoe -f -Z9 -z9 -B10 -r5.0000"], bakes
                assert joins == [
                    join + "-Z12 -o slim-whole.pmtiles slim.pmtiles",
                    join + "-z8 -o low.pmtiles full.pmtiles",
                    join + "-o output.pmtiles low.pmtiles slim-z11.pmtiles"
                           " slim-z10.pmtiles slim-z9.pmtiles"
                           " slim-whole.pmtiles high.pmtiles"], joins

                # a sparse archive's over-cap zoom is thinned by the same
                # rule, the fitting zooms above it cut whole from the probe
                bakes, joins = bake_dense("sparse-over", {
                    "sparse.pmtiles": {11: 2_000_000, 12: 450_000},
                    "sparse-z11.pmtiles": {11: 400_000}}, max_zoom=13)
                assert bakes == [
                    "tippecanoe -f -Z0 -z13 -B14 -r1.0000",
                    *["tippecanoe -f -Z%d -z%d -B%d -r5.0000"
                      % (zoom, zoom, zoom + 1) for zoom in range(11, -1, -1)]
                ], bakes
                assert joins == [
                    join + "-Z12 -o sparse-whole.pmtiles sparse.pmtiles",
                    join + "-o output.pmtiles "
                    + " ".join("sparse-z%d.pmtiles" % zoom
                               for zoom in range(11, -1, -1))
                    + " sparse-whole.pmtiles"], joins

                # a zoom no rate brings under the cap gets the capped bake
                bake_dense("over-anyway", {
                    "high.pmtiles": {14: 900_000},
                    "high-z14.pmtiles": {14: 900_000}})
                with open(calls_path) as f:
                    z14_bakes = [call for call in f.read().splitlines()
                                 if call.startswith("tippecanoe -f -Z14 -z14")]
                assert len(z14_bakes) == 6, z14_bakes
                assert all(" --no-tile-size-limit " in call
                           for call in z14_bakes[:-1]), z14_bakes
                assert " --no-tile-size-limit " not in z14_bakes[-1], z14_bakes

                # a failing tile-join fails the job and leaves no part files
                write_header(14)
                with open(tile_join_path, "w") as f:
                    f.write('#!/bin/sh\necho "join broke" >&2\nexit 1\n')
                create_job({"id": "join-fails", "query": "way;out;",
                            "state": "queued", "startedAt": now_iso()})
                run_job("join-fails")
                failed = read_job("join-fails")
                assert failed["state"] == "failed", failed
                assert failed["error"] == "tile-join: join broke", failed
                assert not [f for f in os.listdir(job_directory("join-fails"))
                            if f.endswith(".pmtiles")], \
                    os.listdir(job_directory("join-fails"))

                # DELETE mid tile-join: the job unwinds, tile-join is killed
                pid_path = os.path.join(scratch_dir, "tile-join.pid")
                with open(tile_join_path, "w") as f:
                    f.write('#!/bin/sh\necho $$ > %s\nexec sleep 30\n'
                            % pid_path)
                create_job({"id": "join-abort", "query": "way;out;",
                            "state": "queued", "startedAt": now_iso()})
                baking = threading.Thread(target=run_job, args=("join-abort",))
                baking.start()
                deadline = time.time() + 10
                while not os.path.exists(pid_path):
                    assert time.time() < deadline, "tile-join never started"
                    time.sleep(0.05)
                shutil.rmtree(job_directory("join-abort"))
                baking.join(10)
                assert not baking.is_alive(), "abort never reached tile-join"
                with open(pid_path) as f:
                    tile_join_pid = int(f.read())
                try:
                    os.kill(tile_join_pid, 0)
                    raise AssertionError("tile-join orphaned")
                except ProcessLookupError:
                    pass
            finally:
                os.environ["PATH"] = old_path
                data_server.shutdown()
                data_server.server_close()
                config.OVERPASS = old_overpass
        finally:
            config.SPOOL = old_spool

        # validate_pmtiles: garbage must not pass as an archive
        bad = os.path.join(scratch_dir, "bad.pmtiles")
        with open(bad, "wb") as f:
            f.write(b"nope")
        try:
            validate_pmtiles(bad)
            raise AssertionError("garbage archive must fail validation")
        except StageError:
            pass
        # a well-formed v3 header addressing zero tiles is an empty bake
        with open(bad, "wb") as f:
            f.write(b"PMTiles\x03" + bytes(119))
        try:
            validate_pmtiles(bad)
            raise AssertionError("zero-tile archive must fail validation")
        except StageError as e:
            assert str(e) == "bake produced an archive with no tiles", e

        # tail_remark: a top-level remark (2-space indent) is a runtime
        # error; the OSM tag remark=* on an element is data, not an error
        remark_tail_path = os.path.join(scratch_dir, "tail.json")
        with open(remark_tail_path, "w", encoding="utf-8") as f:
            f.write('{\n  "elements": [\n{\n  "type": "node",\n'
                    '  "tags": {\n    "remark": "corner note"\n  }\n}\n  ],\n'
                    '  "remark": "runtime error: Query timed out"\n}\n')
        assert tail_remark(remark_tail_path) == "runtime error: Query timed out"
        with open(remark_tail_path, "w", encoding="utf-8") as f:
            f.write('{\n  "elements": [\n{\n  "type": "node",\n'
                    '  "tags": {\n    "remark": "corner note"\n  }\n}\n  ]\n}\n')
        assert tail_remark(remark_tail_path) is None

        # head_error: Overpass can answer 200 with its HTML error page, and
        # the converter would blame the response format instead of Overpass
        head_path = os.path.join(scratch_dir, "head.json")
        with open(head_path, "w", encoding="utf-8") as f:
            f.write(ERROR_PAGE)
        head_text = head_error(head_path)
        assert head_text.startswith("Error : runtime error:"), head_text
        assert classify_error(head_text) == "too_large", head_text
        # anything else is data, however it is shaped: no false error
        for body in ('{\n  "elements": [\n{\n  "type": "node"\n}\n  ]\n}\n',
                     # markup with no "Error:" line: an [out:xml] body keeps
                     # the converter's format complaint, which says more than
                     # the copyright boilerplate html_error would return
                     '<?xml version="1.0"?>\n<osm version="0.6">\n<note>The'
                     ' data included in this document is from OSM.</note>\n'
                     '<node id="1" lat="1.0" lon="1.0"/>\n</osm>\n',
                     ""):  # a truncated or empty body is not an error page
            with open(head_path, "w", encoding="utf-8") as f:
                f.write(body)
            assert head_error(head_path) is None, body[:40]
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    print("selftest ok")
