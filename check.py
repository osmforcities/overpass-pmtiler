#!/usr/bin/env python3
"""End-to-end check: one small-city job through POST -> fetch -> convert -> bake.

Starts its own pmtiler on a free port with a scratch spool, runs the job against
a live Overpass, asserts the contract, tears everything down. Exits nonzero on
the first failed assertion.

    PMTILER_OVERPASS=https://localhost:8080/api/interpreter PMTILER_INSECURE=1 \
        python3 check.py

Defaults to the osmforcities dev tunnel (infra/ansible/scripts/overpass-tunnel.sh).
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

HERE = os.path.dirname(os.path.abspath(__file__))
SPOOL = os.path.join(HERE, ".check-spool")
OVERPASS = os.environ.get("PMTILER_OVERPASS", "https://localhost:8080/api/interpreter")
INSECURE = os.environ.get("PMTILER_INSECURE", "1")
DEADLINE = float(os.environ.get("CHECK_TIMEOUT", "900"))

# Delft, rel 324269 -- ~39.5k buildings: small enough for a ~1 min run, big
# enough to exercise the real area-query path.
JOB = {
    "id": "check-delft-buildings",
    "query": '[out:json];area(3600324269)->.a;way["building"](area.a);out geom meta;',
    "timeout": 300,
    "maxsize": 536870912,
}
EXPECTED_FEATURES = 39576
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
    """Poll to a terminal state, collecting every state seen on the way."""
    seen = []
    end = time.time() + DEADLINE
    while time.time() < end:
        status, job = get_json(base, "/jobs/" + jid)
        assert status == 200, (status, job)
        if not seen or seen[-1] != job["state"]:
            seen.append(job["state"])
            print("  state:", job["state"], flush=True)
        if job["state"] in ("done", "failed"):
            return job, seen
        time.sleep(2)
    raise AssertionError("job %s stuck in %s after %ss" % (jid, seen[-1], DEADLINE))


def main():
    shutil.rmtree(SPOOL, ignore_errors=True)
    os.makedirs(SPOOL)
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "pmtiler.py")],
        env=dict(os.environ, PMTILER_SPOOL=SPOOL, PMTILER_PORT=str(port),
                 PMTILER_OVERPASS=OVERPASS, PMTILER_INSECURE=INSECURE))
    try:
        for _ in range(50):
            try:
                call(base, "GET", "/jobs/nope")
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("service did not come up on %s" % base)
        print("pmtiler up on", base, "overpass:", OVERPASS)

        # --- happy path
        status = call(base, "POST", "/jobs", JOB)[0]
        assert status == 202, "POST /jobs returned %s" % status
        print("submitted", JOB["id"])
        job, seen = poll(base, JOB["id"])
        assert job["state"] == "done", "job failed: %s" % job.get("error")
        assert set(seen) <= STATES, seen
        assert "fetching" in seen and "done" in seen, seen

        stats = job["stats"]
        assert abs(stats["features"] - EXPECTED_FEATURES) < EXPECTED_FEATURES * 0.2, \
            "unexpected feature count %s" % stats["features"]
        assert stats["ways"] == stats["features"], stats
        assert stats["editorsCount"] > 0 and stats["changesetsCount"] > 0, stats
        w, s, e, n = stats["bbox"]
        assert 4.2 < w < e < 4.5 and 51.9 < s < n < 52.1, stats["bbox"]
        assert sum(stats["editRecencyBands"]) == stats["features"], stats
        print("stats: %d features, %d editors, %d changesets" % (
            stats["features"], stats["editorsCount"], stats["changesetsCount"]))

        # --- outputs
        status, _, sj = call(base, "GET", "/jobs/%s/stats.json" % JOB["id"])
        assert status == 200 and json.loads(sj)["features"] == stats["features"]

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
        shutil.rmtree(SPOOL, ignore_errors=True)


if __name__ == "__main__":
    main()
