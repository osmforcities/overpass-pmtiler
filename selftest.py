"""Self-check for the converter and the spool machinery, plus fetch
transport failures against loopback sockets only -- no external network, no
tippecanoe. Run via `python3 pmtiler.py --selftest`. Plain asserts, one
fixture, no framework."""
import json
import os
import http.server
import shutil
import socket
import threading
import time

import config
import overpass
from bake import PROGRESS_RE, validate_pmtiles
from config import DAY, now_iso
from convert import brace_delta, convert, is_area
from errors import Aborted, StageError
from overpass import (apply_settings, classify_error, head_error,
                      html_error, tail_remark)
from runner import Q, recover, run_job
from spool import (job_directory, list_jobs, progress_writer, read_job,
                   sweep, write_job)


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

        # abort seam: a stale run must strand on its next write, both when the
        # dir is gone and when a re-POST recreated it (generation mismatch)
        old_spool, config.SPOOL = config.SPOOL, os.path.join(scratch_dir, "spool")
        try:
            job = {"id": "j1", "state": "queued", "startedAt": now_iso()}
            os.makedirs(job_directory("j1"))
            write_job(job)
            shutil.rmtree(job_directory("j1"))
            try:
                write_job(job)
                raise AssertionError("deleted dir must abort")
            except Aborted:
                pass
            os.makedirs(job_directory("j1"))
            write_job({"id": "j1", "state": "queued", "startedAt": now_iso()})
            try:
                write_job(job)
                raise AssertionError("recreated id must abort the stale run")
            except Aborted:
                pass

            # progress_writer: first tick always writes, the throttle
            # suppresses, and a tick on a deleted job is an abort checkpoint
            progress_job = {"id": "j2", "state": "fetching", "startedAt": now_iso()}
            os.makedirs(job_directory("j2"))
            write_job(progress_job)
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
                os.makedirs(job_directory(jid))
                write_job({"id": jid, "state": state, "startedAt": ts,
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
                    os.makedirs(job_directory(jid))
                    write_job({"id": jid, "query": "way;out;", "timeout": 1,
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
