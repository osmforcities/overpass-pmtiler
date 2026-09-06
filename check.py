#!/usr/bin/env python3
"""End-to-end check: one small-city job through POST -> fetch -> convert -> bake.

Starts its own pmtiler on a free port with a scratch spool, runs the job against
a live Overpass, asserts the contract, tears everything down. Exits nonzero on
the first failed assertion.

    PMTILER_OVERPASS=https://localhost:8080/api/interpreter PMTILER_INSECURE=1 \
        python3 check.py

    python3 check.py --offline

Defaults to the osmforcities dev tunnel (infra/ansible/scripts/overpass-tunnel.sh).
--offline replaces the tunnel with an in-process stub Overpass serving the
selftest fixture, so the whole HTTP contract runs without network or an
Overpass (tippecanoe still required); only the live-data assertions are
dropped, replaced with fixture literals.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

LIVE = "--offline" not in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SPOOL = os.path.join(HERE, ".check-spool")
OVERPASS = os.environ.get("PMTILER_OVERPASS", "https://localhost:8080/api/interpreter")
INSECURE = os.environ.get("PMTILER_INSECURE", "1")
DEADLINE = float(os.environ.get("CHECK_TIMEOUT", "900"))
POLL = 2.0 if LIVE else 0.25

if not LIVE:
    import http.server
    import threading
    import urllib.parse

    import pmtiler

    # Offline expectations are literals pinned to pmtiler.FIXTURE (7 features).
    # Never derive them from convert() -- that would make the check tautological.
    OFFLINE_FEATURES = 7
    OFFLINE_BBOX = [-1.0, -1.0, 24.5, 14.5]
    OFFLINE_RELATIONS = (3, 3)      # (relations, relationsSkipped)
    OFFLINE_UNDATED = 3             # two timestamp-less relations + member node

    class StubOverpass(http.server.BaseHTTPRequestHandler):
        """Answers like Overpass: the fixture for any query with an out clause,
        an Overpass-shaped HTML error page for anything the service rejects."""

        protocol_version = "HTTP/1.0"  # Content-Length framing, no keep-alive

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            query = form.get("data", [""])[0]
            # Drop the leading [out:json][...]; settings statement the service
            # prepends (apply_settings) before looking for an out clause --
            # otherwise "out:json" satisfies any query.
            end = query.find(";")
            if query.startswith("[") and end != -1:
                query = query[end + 1:]
            if "out" in query:
                # Pad past 1 MB: the service reads in 1 MiB chunks and a
                # buffered read blocks until the chunk is full, so the first
                # write below must satisfy a whole read() for a progress tick
                # to land mid-fetch where a poll can sample it. Trailing
                # newlines are inert to the converter (it stops at the
                # elements array's closing bracket).
                body = pmtiler.FIXTURE.encode() + b"\n" * (2 * 1024 * 1024)
                split = 1024 * 1024 + 512
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[:split])
                self.wfile.flush()
                time.sleep(0.5)  # hold fetching open for the polls
                self.wfile.write(body[split:])
            else:
                body = ('<html><p>copyright boilerplate</p>'
                        '<p><b>Error</b>: line 1: parse error - Unknown type '
                        '&quot;this&quot;</p></html>').encode()
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *a):
            pass

    def start_stub():
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubOverpass)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d/api/interpreter" % httpd.server_address[1], httpd

# Delft, rel 324269 -- ~39.5k buildings: small enough for a ~1 min run, big
# enough to exercise the real area-query path. nwr matches the app's query
# shape (all three element types), so relation assembly runs end-to-end.
JOB = {
    "id": "check-delft-buildings",
    "query": '[out:json];area(3600324269)->.a;nwr["building"](area.a);out geom meta;',
    "timeout": 300,
    "maxsize": 536870912,
    "filterableTags": ["building", "amenity"],
}
EXPECTED_FEATURES = 39627
STATES = {"queued", "fetching", "converting", "baking", "done", "failed"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_json(base, path):
    status, _, body = call(base, "GET", path)
    return status, json.loads(body)


def poll(base, jid):
    """Poll to a terminal state, collecting states and progress seen on the way."""
    seen, progress = [], []
    end = time.time() + DEADLINE
    while time.time() < end:
        status, job = get_json(base, "/jobs/" + jid)
        assert status == 200, (status, job)
        if job.get("progress"):
            progress.append(job["progress"])
        if not seen or seen[-1] != job["state"]:
            seen.append(job["state"])
            print("  state:", job["state"], flush=True)
        if job["state"] in ("done", "failed"):
            job["_progress"] = progress
            return job, seen
        time.sleep(POLL)
    raise AssertionError("job %s stuck in %s after %ss" % (jid, seen[-1], DEADLINE))


def main():
    stub = None
    overpass = OVERPASS
    if not LIVE:
        overpass, stub = start_stub()
    shutil.rmtree(SPOOL, ignore_errors=True)
    os.makedirs(SPOOL)
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "pmtiler.py")],
        env=dict(os.environ, PMTILER_SPOOL=SPOOL, PMTILER_PORT=str(port),
                 PMTILER_OVERPASS=overpass, PMTILER_INSECURE=INSECURE))
    try:
        for _ in range(50):
            try:
                call(base, "GET", "/jobs/nope")
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("service did not come up on %s" % base)
        print("pmtiler up on", base, "overpass:", overpass)

        # --- happy path
        status = call(base, "POST", "/jobs", JOB)[0]
        assert status == 202, "POST /jobs returned %s" % status
        print("submitted", JOB["id"])
        job, seen = poll(base, JOB["id"])
        assert job["state"] == "done", "job failed: %s" % job.get("error")
        assert set(seen) <= STATES, seen
        assert "fetching" in seen and "done" in seen, seen

        # progress is reported per stage, and cleared on the state change
        assert job["_progress"], "no progress was ever reported"
        assert any(p["stage"] == "fetching" and p["bytes"] > 0
                   for p in job["_progress"]), job["_progress"]
        # convert may be too fast to sample here; when caught it must be a true pct
        assert all(0 <= p["pct"] <= 100 for p in job["_progress"] if "pct" in p), \
            job["_progress"]
        assert "progress" not in job, "progress must be cleared on a terminal state"
        print("progress samples:", len(job["_progress"]),
              "stages:", sorted({p["stage"] for p in job["_progress"]}))

        stats = job["stats"]
        if LIVE:
            assert abs(stats["features"] - EXPECTED_FEATURES) < EXPECTED_FEATURES * 0.2, \
                "unexpected feature count %s" % stats["features"]
        else:
            assert stats["features"] == OFFLINE_FEATURES, stats["features"]
        assert stats["nodes"] + stats["ways"] + stats["relations"] \
            == stats["features"], stats
        assert stats["relations"] > 0, "nwr query must assemble relations"
        if LIVE:
            assert stats["relationsSkipped"] < stats["relations"], stats
        else:
            assert (stats["relations"], stats["relationsSkipped"]) \
                == OFFLINE_RELATIONS, stats
        assert stats["editorsCount"] > 0 and stats["changesetsCount"] > 0, stats
        w, s, e, n = stats["bbox"]
        if LIVE:
            assert 4.2 < w < e < 4.5 and 51.9 < s < n < 52.1, stats["bbox"]
        else:
            assert [w, s, e, n] == OFFLINE_BBOX, stats["bbox"]
        # only emitted member nodes (no meta) may be undated; the fixture also
        # carries two timestamp-less relations, hence the literal offline
        undated = stats["features"] - sum(stats["editRecencyBands"])
        if LIVE:
            assert 0 <= undated <= stats["nodes"], stats
        else:
            assert undated == OFFLINE_UNDATED, stats
        print("stats: %d features, %d editors, %d changesets" % (
            stats["features"], stats["editorsCount"], stats["changesetsCount"]))

        # filterDimensions follow the job's filterableTags, in order
        dims = stats["filterDimensions"]
        assert [d["key"] for d in dims] == JOB["filterableTags"], dims
        building = dims[0]
        assert building["kind"] == "tag" and building["values"], building
        assert building["values"] == sorted(
            building["values"], key=lambda v: -v["count"]), "not sorted desc"
        # every non-missing feature contributes at least one value bucket
        assert sum(v["count"] for v in building["values"]) \
            >= stats["features"] - building["missing"], building["values"][:3]
        # every feature matched building=* except emitted member nodes
        if LIVE:  # the fixture's corpus is not building=*
            assert building["missing"] <= stats["nodes"], building["missing"]
        assert dims[1]["missing"] > 0, dims[1]  # amenity: sparse, entry kept
        mix = stats["geometryMix"]
        assert mix["points"] + mix["lines"] + mix["areas"] == stats["features"], mix
        assert mix["areas"] > mix["lines"] and mix["areas"] > mix["points"], mix
        assert mix["areaKm2"] > 0 and mix["lineKm"] >= 0, mix
        print("dims: building top=%s(%d) missing=%d; mix: %d areas, %.2f km2" % (
            building["values"][0]["value"], building["values"][0]["count"],
            building["missing"], mix["areas"], mix["areaKm2"]))

        # --- outputs
        status, _, sj = call(base, "GET", "/jobs/%s/stats.json" % JOB["id"])
        assert status == 200 and json.loads(sj)["features"] == stats["features"]
        # the served file is the cross-repo surface the app consumer will pin
        assert json.loads(sj)["schemaVersion"] == 1, sj[:200]

        status, _, nd = call(base, "GET", "/jobs/%s/data.ndjson" % JOB["id"])
        assert status == 200, status
        first = json.loads(nd.split(b"\n", 1)[0])
        assert first["type"] == "Feature", first
        assert isinstance(first["properties"]["_ts"], int), "_ts must be numeric epoch"

        status, headers, archive = call(base, "GET", "/jobs/%s/output.pmtiles" % JOB["id"])
        assert status == 200, status
        assert archive[:7] == b"PMTiles" and archive[7] == 3, archive[:8]
        assert int.from_bytes(archive[72:80], "little") > 0, "archive has no tiles"
        assert headers.get("Accept-Ranges") == "bytes"
        print("archive: %d bytes, %d tiles" % (
            len(archive), int.from_bytes(archive[72:80], "little")))

        # --- range requests (every pmtiles client is nothing but these)
        status, headers, part = call(
            base, "GET", "/jobs/%s/output.pmtiles" % JOB["id"],
            headers={"Range": "bytes=0-99"})
        assert status == 206, status
        assert len(part) == 100 and part == archive[:100], len(part)
        assert headers["Content-Range"] == "bytes 0-99/%d" % len(archive)

        # --- idempotent resubmit: same id, current state, no re-run
        status, _, body = call(base, "POST", "/jobs", JOB)
        assert status == 200 and json.loads(body)["state"] == "done", body

        # --- validation at the trust boundary
        assert call(base, "POST", "/jobs", {"id": "../escape", "query": "x"})[0] == 400
        assert call(base, "POST", "/jobs", {"id": "ok", "query": ""})[0] == 400
        assert call(base, "POST", "/jobs",
                    {"id": "ok", "query": "x", "timeout": 99999})[0] == 400
        assert call(base, "POST", "/jobs",
                    {"id": "ok", "query": "x",
                     "filterableTags": "building"})[0] == 400

        # --- overpass errors surface verbatim
        bad = {"id": "check-broken", "query": "this is not overpass ql", "timeout": 30}
        assert call(base, "POST", "/jobs", bad)[0] == 202
        job, _ = poll(base, bad["id"])
        assert job["state"] == "failed", job
        assert job.get("error", "").startswith("Error"), job  # not the boilerplate
        assert "parse error" in job["error"], job
        print("failure path:", job["error"][:120])

        # --- delete = ack + cleanup
        assert call(base, "DELETE", "/jobs/" + JOB["id"])[0] == 200
        assert call(base, "GET", "/jobs/" + JOB["id"])[0] == 404
        assert not os.path.exists(os.path.join(SPOOL, JOB["id"]))

        print("\nOK: POST -> fetch -> convert -> bake -> done, outputs served")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        if stub:
            stub.shutdown()
            stub.server_close()
        shutil.rmtree(SPOOL, ignore_errors=True)


if __name__ == "__main__":
    main()
