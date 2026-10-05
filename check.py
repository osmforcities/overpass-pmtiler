#!/usr/bin/env python3
"""End-to-end check: one small-city job through POST -> fetch -> convert -> bake.

Starts its own pmtiler on a free port with a scratch spool, runs the job against
a live Overpass, asserts the contract, tears everything down. Exits nonzero on
the first failed assertion.

    PMTILER_OVERPASS=https://localhost:8080/api/interpreter PMTILER_INSECURE=1 \
        python3 check.py

    python3 check.py --offline

Defaults to an Overpass tunnelled to https://localhost:8080 with a self-signed
cert (hence PMTILER_INSECURE=1); set PMTILER_OVERPASS to point elsewhere.
--offline replaces the tunnel with an in-process stub Overpass serving the
selftest fixture, so the whole HTTP contract runs without network or an
Overpass (tippecanoe still required); only the live-data assertions are
dropped, replaced with fixture literals. CI (.github/workflows/check.yml)
runs the selftest and the offline mode on every push; live stays a manual
pre-deploy check.

Canonical test areas (any Overpass instance with area support; also the
one-click buttons in viewer.html):

    case            area                    relation  size            measured
    small           Delft                   324269    39.6k buildings 25s, 3.8 MB
    mid             Amsterdam               271110    197k buildings  ~1 min
    standard metro  Sao Paulo (municipality) 298285   2.14M buildings 9m30s, 53 MB
    street grid     Sao Paulo highway=*     298285    174k ways       ~90s, 10.6 MB
    large state     Sao Paulo (state)       298204    2.71M buildings not yet run
    ceiling         Tokyo Metropolis        1543125   >3 GiB to count fails: too_large

Tokyo stays on purpose: refusing it gracefully is a requirement, so the
ceiling case must be as easy to run as the happy ones. The street grid is
the line-geometry counterpart to the building runs (2026-09 measurement:
173,470 LineStrings + 712 areas, 23,036 lineKm, convert 6.9s at 46 MB
RSS) -- a metro's streets are ~20x cheaper than its buildings.

Reference measurements (2026-09, a multi-core server), Sao Paulo
building=* end to end ~10 min: fetch 2.23 GB in 312s at 768 MiB maxsize
(the default 512 MiB fails metro-scale area evaluation), stream-convert
64s at 13 MB peak RSS, bake 194s at 1.2 GB peak RSS, output 53 MB PMTiles
(42:1 vs wire). Plan ~3 GB transient disk per metro job. Reproduced on a
laptop over the SSH tunnel within 5% at every stage -- the pipeline is not
sensitive to where it runs.
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
            if "huge_area" in query:
                # over-ceiling: Overpass answers 200 with a runtime remark
                body = ('{\n  "elements": [\n  ],\n  "remark": "runtime error:'
                        ' Query run out of memory using about 2048 MB of'
                        ' RAM."\n}\n').encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif "dispatcher_error" in query:
                # the other over-ceiling shape: status 200, but the body is
                # Overpass's HTML error page rather than [out:json] data
                body = pmtiler.ERROR_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif "nothing_matches" in query:
                body = b'{\n  "elements": [\n  ]\n}\n'  # valid, zero elements
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif "out" in query:
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
                try:
                    self.wfile.write(body[split:])
                except ConnectionError:
                    pass  # the recovery scenario kills the service mid-read
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
    "keepMeta": True,
    "ageBandsDays": [7, 30, 90],
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
    service_env = dict(os.environ, PMTILER_SPOOL=SPOOL, PMTILER_PORT=str(port),
                       PMTILER_OVERPASS=overpass, PMTILER_INSECURE=INSECURE)

    def spawn_service():
        started = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "pmtiler.py")],
            env=service_env)
        for _ in range(50):
            try:
                call(base, "GET", "/jobs/nope")
                return started
            except OSError:
                time.sleep(0.2)
        raise AssertionError("service did not come up on %s" % base)

    proc = spawn_service()
    try:
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
        # a nonzero skipped means the element splitter lost data silently
        assert stats["skipped"] == 0, stats
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
        # ageBandsDays was requested: cutoffs+1 per-feature buckets
        age_bands = json.loads(sj)["ageBands"]
        assert len(age_bands) == 4, age_bands
        assert sum(age_bands) == stats["features"], age_bands

        status, _, nd = call(base, "GET", "/jobs/%s/data.ndjson" % JOB["id"])
        assert status == 200, status
        first = json.loads(nd.split(b"\n", 1)[0])
        assert first["type"] == "Feature", first
        assert isinstance(first["properties"]["_ts"], int), "_ts must be numeric epoch"
        # keepMeta was requested: editor metadata rides in the ndjson
        assert b'"@user"' in nd and b'"@timestamp"' in nd, "keepMeta absent"

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

        # the edges real clients hit: suffix, open-ended, unsatisfiable,
        # malformed (full body per RFC)
        arc = "/jobs/%s/output.pmtiles" % JOB["id"]
        status, headers, part = call(base, "GET", arc,
                                     headers={"Range": "bytes=-100"})
        assert status == 206 and part == archive[-100:], status
        assert headers["Content-Range"] == "bytes %d-%d/%d" % (
            len(archive) - 100, len(archive) - 1, len(archive))
        status, _, part = call(base, "GET", arc,
                               headers={"Range": "bytes=100-"})
        assert status == 206 and part == archive[100:], status
        status, headers, part = call(base, "GET", arc,
                                     headers={"Range": "bytes=-0"})
        assert status == 416, status
        assert headers["Content-Range"] == "bytes */%d" % len(archive)
        status, _, part = call(base, "GET", arc,
                               headers={"Range": "bytes=nope"})
        assert status == 200 and part == archive, status

        # --- idempotent resubmit: same id, current state, no re-run
        status, _, body = call(base, "POST", "/jobs", JOB)
        assert status == 200 and json.loads(body)["state"] == "done", body

        # --- validation at the trust boundary
        assert call(base, "POST", "/jobs", {"id": "../escape", "query": "x"})[0] == 400
        # "." and ".." match the id charset but resolve outside the spool:
        # DELETE /jobs/.. would rmtree the spool's PARENT if these passed
        assert call(base, "POST", "/jobs", {"id": "..", "query": "x"})[0] == 400
        assert call(base, "POST", "/jobs", {"id": ".", "query": "x"})[0] == 400
        assert call(base, "DELETE", "/jobs/..")[0] == 404
        assert os.path.isdir(SPOOL), "spool parent must survive DELETE /jobs/.."
        assert call(base, "POST", "/jobs", {"id": "ok", "query": ""})[0] == 400
        assert call(base, "POST", "/jobs",
                    {"id": "ok", "query": "x", "timeout": 99999})[0] == 400
        assert call(base, "POST", "/jobs",
                    {"id": "ok", "query": "x",
                     "filterableTags": "building"})[0] == 400
        assert call(base, "POST", "/jobs",
                    {"id": "ok", "query": "x", "keepMeta": "yes"})[0] == 400
        assert call(base, "POST", "/jobs",
                    {"id": "ok", "query": "x",
                     "ageBandsDays": [30, 7]})[0] == 400  # must ascend
        assert call(base, "POST", "/jobs",
                    {"id": "ok", "query": "x",
                     "ageBandsDays": []})[0] == 400  # empty list is a mistake

        # --- overpass errors surface verbatim
        bad = {"id": "check-broken", "query": "this is not overpass ql", "timeout": 30}
        assert call(base, "POST", "/jobs", bad)[0] == 202
        job, _ = poll(base, bad["id"])
        assert job["state"] == "failed", job
        assert job.get("error", "").startswith("Error"), job  # not the boilerplate
        assert "parse error" in job["error"], job
        print("failure path:", job["error"][:120])

        # --- zero-feature query fails with the real cause, not a bake error
        if not LIVE:
            empty = {"id": "check-empty",
                     "query": "nothing_matches out;", "timeout": 30}
            assert call(base, "POST", "/jobs", empty)[0] == 202
            job, _ = poll(base, empty["id"])
            assert job["state"] == "failed", job
            assert job.get("error") == "query matched no elements", job

        # --- over-ceiling failures carry a machine-readable errorKind;
        # unclassified failures (check-broken above) carry none
        if not LIVE:
            huge = {"id": "check-huge",
                    "query": "huge_area out;", "timeout": 30}
            assert call(base, "POST", "/jobs", huge)[0] == 202
            job, _ = poll(base, huge["id"])
            assert job["state"] == "failed", job
            assert job.get("errorKind") == "too_large", job
            assert "out of memory" in job.get("error", ""), job
            broken_job = json.loads(call(base, "GET", "/jobs/check-broken")[2])
            assert "errorKind" not in broken_job, broken_job

            # a 200 HTML error page must fail in the fetch stage with
            # Overpass's own text (and classify), not reach the converter
            page = {"id": "check-html-error",
                    "query": "dispatcher_error out;", "timeout": 30}
            assert call(base, "POST", "/jobs", page)[0] == 202
            job, seen = poll(base, page["id"])
            assert job["state"] == "failed", job
            assert job.get("errorKind") == "too_large", job
            assert "Dispatcher_Client" in job.get("error", ""), job
            assert "response format" not in job.get("error", ""), job
            assert "converting" not in seen, seen

        # --- one malformed job.json must not 500 the list endpoint
        os.makedirs(os.path.join(SPOOL, "malformed"), exist_ok=True)
        with open(os.path.join(SPOOL, "malformed", "job.json"), "w") as f:
            f.write('{"x": 1}')
        status, _, body = call(base, "GET", "/jobs")
        assert status == 200, status
        rows = json.loads(body)
        assert any(row["id"] == JOB["id"] for row in rows), body[:200]
        # optional fields are omitted when absent, never null -- the same
        # shape as GET /jobs/:id
        for row in rows:
            assert "progress" not in row or row["progress"], row
            assert "errorKind" not in row or row["errorKind"], row
        if not LIVE:  # check-huge (the too_large job) only exists offline
            assert any(row.get("errorKind") == "too_large"
                       for row in rows), rows
        shutil.rmtree(os.path.join(SPOOL, "malformed"))

        # --- /status: the dashboard + monitoring surface
        status, snapshot = get_json(base, "/status")
        assert status == 200, status
        assert snapshot["queueDepth"] == 0, snapshot
        assert snapshot["jobStates"].get("done", 0) >= 1, snapshot
        assert snapshot["jobStates"].get("failed", 0) >= 1, snapshot
        assert snapshot["spoolBytes"] > 0, snapshot
        assert snapshot["diskFreeBytes"] > 0, snapshot
        assert snapshot["uptimeSeconds"] >= 0, snapshot

        # --- crash recovery (#9): kill mid-run, restart, jobs resume FIFO
        if not LIVE:
            # seed a job that "crashed mid-bake long ago": recover() must
            # run it FIRST (FIFO by startedAt) and tippecanoe -f must
            # overwrite the partial archive the crash left behind
            seeded = "check-recover-bake"
            os.makedirs(os.path.join(SPOOL, seeded), exist_ok=True)
            with open(os.path.join(SPOOL, seeded, "job.json"), "w") as f:
                json.dump({"id": seeded, "query": JOB["query"],
                           "maxsize": None, "timeout": 30,
                           "filterableTags": None, "state": "baking",
                           "startedAt": "2000-01-01T00:00:00+00:00",
                           "error": None, "stats": None}, f)
            with open(os.path.join(SPOOL, seeded, "output.pmtiles"), "wb") as f:
                f.write(b"partial junk left by the interrupted bake")

            killed = "check-recover-fetch"
            assert call(base, "POST", "/jobs",
                        {"id": killed, "query": JOB["query"],
                         "timeout": 30})[0] == 202
            end = time.time() + DEADLINE
            while get_json(base, "/jobs/" + killed)[1]["state"] == "queued":
                assert time.time() < end, "job never started"
                time.sleep(0.05)
            proc.kill()  # SIGKILL: no goodbye, like a crash or an OOM kill
            proc.wait()
            with open(os.path.join(SPOOL, killed, "job.json")) as f:
                interrupted = json.load(f)["state"]
            assert interrupted in ("fetching", "converting", "baking"), \
                interrupted  # exactly the limbo recover() exists to fix

            proc = spawn_service()  # same spool, same port
            job, _ = poll(base, seeded)
            assert job["state"] == "done", job
            job, _ = poll(base, killed)
            assert job["state"] == "done", job
            # FIFO: the seeded job (older startedAt) baked first
            assert (os.path.getmtime(os.path.join(SPOOL, seeded, "stats.json"))
                    <= os.path.getmtime(os.path.join(SPOOL, killed,
                                                     "stats.json")))
            with open(os.path.join(SPOOL, seeded, "output.pmtiles"), "rb") as f:
                assert f.read(7) == b"PMTiles", \
                    "partial archive must be overwritten on replay"
            print("recovery: %s replayed from %s, both done, FIFO held"
                  % (killed, interrupted))

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
