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
BANDS = (90 * DAY, 365 * DAY, 730 * DAY)  # app dataset-recency.ts cutoffs

Q = queue.Queue()
CREATE_LOCK = threading.Lock()


class StageError(Exception):
    """Carries user-visible error text into the job's `failed` state."""


class Aborted(Exception):
    """Job was deleted while running."""


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
    except (OSError, ValueError):
        return None


def write_job(job):
    jd = jobdir(job["id"])
    if not os.path.isdir(jd):
        raise Aborted(job["id"])
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
    """Throttled progress into job.json -- callers poll state, nothing is pushed.

    Also the abort signal: write_job raises Aborted once the job dir is gone, so
    a DELETE stops the running stage instead of waiting for the next boundary.
    """
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
            job["state"] = "queued"
            write_job(job)
            log("recovered", name)
        if job.get("state") == "queued":
            pending.append(job)
    for job in sorted(pending, key=lambda j: j.get("startedAt") or ""):
        Q.put(job["id"])


# --- fetch -----------------------------------------------------------------

SETTING_RE = re.compile(r"\[\s*(?:maxsize|timeout)\s*:\s*\d+\s*\]", re.I)
REMARK_RE = re.compile(r'"remark"\s*:\s*"((?:[^"\\]|\\.)*)"')
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
    remark = tail_remark(path)
    if remark:
        raise StageError(remark)
    log("fetched", job["id"], os.path.getsize(path), "bytes")


# --- convert ---------------------------------------------------------------

def convert(inp, outp, tick=None):
    """Overpass JSON -> nd-geojson + accumulator stats in one bounded-memory pass.

    Splits the elements array on element boundaries by brace depth (line-based;
    Overpass pretty-prints), then json.loads per element: 13 MB RSS at 2.23 GB in.
    Ported from spike/convert_fast.py.
    """
    t0 = time.time()
    now = int(time.time())
    counts = {"nodes": 0, "ways": 0, "relationsSkipped": 0, "skipped": 0}
    editors, changesets = set(), set()
    recent_editors, recent_changesets = set(), set()
    mapper_latest, tag_counts = {}, {}
    edit_bands = [0, 0, 0, 0]
    version_sum = age_sum = dated = recent_edits = stale = fresh = 0
    oldest = newest = None
    bbox = [180.0, 90.0, -180.0, -90.0]

    with open(inp, encoding="utf-8") as f, open(outp, "w", encoding="utf-8") as out:
        for line in f:  # skip the header, stop at the elements array
            if line.startswith('  "elements":'):
                break
        depth = 0
        buf = []
        for line in f:
            s = line.strip()
            if depth == 0:
                if s.startswith("{"):
                    depth = s.count("{") - s.count("}")
                    buf = [s]
                    if depth:
                        continue
                    raw = s.rstrip(",")
                elif s.startswith("]"):
                    break
                else:
                    continue
            else:
                buf.append(s)
                depth += s.count("{") - s.count("}")
                if depth > 0:
                    continue
                raw = "".join(buf).rstrip(",")
            try:
                el = json.loads(raw)
            except ValueError:
                counts["skipped"] += 1
                continue

            kind = el.get("type")
            if kind == "way" and el.get("geometry"):
                coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
                closed = len(coords) > 3 and coords[0] == coords[-1]
                geometry = ({"type": "Polygon", "coordinates": [coords]} if closed
                            else {"type": "LineString", "coordinates": coords})
                counts["ways"] += 1
            elif kind == "node" and "lat" in el:
                coords = [[el["lon"], el["lat"]]]
                geometry = {"type": "Point", "coordinates": coords[0]}
                counts["nodes"] += 1
            else:
                # relations/multipolygons are issue #6
                counts["relationsSkipped" if kind == "relation" else "skipped"] += 1
                continue

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

            tags = el.get("tags") or {}
            for key in tags:
                tag_counts[key] = tag_counts.get(key, 0) + 1

            props = dict(tags)
            props["@id"] = "%s/%s" % (kind, el["id"])
            props["_ts"] = ts  # numeric epoch: a string breaks MapLibre `step`
            out.write(json.dumps({"type": "Feature", "properties": props,
                                  "geometry": geometry},
                                 separators=(",", ":")) + "\n")
            if tick:  # element total is unknown until the pass ends
                tick(features=counts["nodes"] + counts["ways"])

    features = counts["nodes"] + counts["ways"]
    mapper_bands = [0, 0, 0, 0]
    for ts in mapper_latest.values():
        mapper_bands[sum((now - ts) > b for b in BANDS)] += 1
    pct = (lambda n: round(100.0 * n / features, 2)) if features else (lambda n: 0.0)
    return {
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
        # ponytail: no geometryMix -- needs geodesic length/area, not accumulator
        # -cheap. Add alongside relation assembly (#6) when the app consumes it.
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


PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")


def stage_bake(job, jd):
    out = os.path.join(jd, "output.pmtiles")
    cmd = ["nice", "-n", NICE, "tippecanoe", "-f", "-zg",
           "--drop-densest-as-needed", "-P", "-l", "features",
           "-o", out, os.path.join(jd, "data.ndjson")]
    # tmp files stay inside the job dir, never /tmp
    p = subprocess.Popen(cmd, cwd=jd, env=dict(os.environ, TMPDIR=jd),
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True)
    # tippecanoe redraws "  NN.N%  z/x/y" on \r; keep a tail for the error text
    tick = progress_writer(job, "baking")
    tail = ""
    while True:
        buf = p.stderr.read(4096)
        if not buf:
            break
        tail = (tail + buf)[-4000:]
        found = PCT_RE.findall(buf)
        if found:
            tick(pct=float(found[-1]))
    if p.wait() != 0:
        raise StageError("tippecanoe: " + (tail.replace("\r", "\n").strip()[-2000:]
                                           or "failed"))
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
                        progress_writer(job, "converting"))
        with open(os.path.join(jd, "stats.json"), "w", encoding="utf-8") as f:
            json.dump(stats, f)
        job["stats"] = stats
        os.remove(raw)  # ~2 GB at metro scale; the nd-geojson supersedes it

        set_state(job, "baking")
        stage_bake(job, jd)
        set_state(job, "done")
    except Aborted:
        log("job", jid, "aborted (deleted)")
    except StageError as e:
        fail(jid, str(e))
    except Exception as e:
        fail(jid, "%s: %s" % (type(e).__name__, e))


def fail(jid, error):
    job = read_job(jid)
    if not job:
        return
    job["error"] = error
    try:
        set_state(job, "failed")
    except Aborted:
        pass
    log("job", jid, "failed:", error[:200])


def worker():
    while True:
        jid = Q.get()
        try:
            run_job(jid)
            sweep()
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

        with CREATE_LOCK:
            existing = read_job(jid)
            if existing:  # idempotent resubmit: report state, never re-run
                return self.send_json(200, {"id": jid, "state": existing["state"]})
            os.makedirs(jobdir(jid), exist_ok=True)
            write_job({"id": jid, "query": query, "maxsize": maxsize,
                       "timeout": timeout, "state": "queued",
                       "startedAt": now_iso(), "error": None, "stats": None})
            Q.put(jid)
        log("job", jid, "queued")
        self.send_json(202, {"id": jid, "state": "queued"})

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/viewer.html"):  # visual inspection, same-origin
            return self.send_file(os.path.join(HERE, "viewer.html"), "text/html")
        if path == "/jobs":
            return self.send_json(200, sorted(
                ({"id": j["id"], "state": j["state"], "startedAt": j["startedAt"],
                  "progress": j.get("progress")}
                 for j in filter(None, map(read_job, os.listdir(SPOOL)))),
                key=lambda j: j["startedAt"], reverse=True))
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
  "tags": { "building": "yes" }
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
  "changeset": 11, "user": "ada", "timestamp": "2026-08-02T00:00:00Z" },
{ "type": "relation", "id": 4, "members": [], "tags": { "building": "yes" } }

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
        s = convert(src, dst)
        with open(dst, encoding="utf-8") as f:
            feats = [json.loads(line) for line in f]

        assert [f["geometry"]["type"] for f in feats] == \
            ["Polygon", "LineString", "Point"], feats
        assert all(isinstance(f["properties"]["_ts"], int) for f in feats)
        assert feats[0]["properties"]["@id"] == "way/1"
        assert s["features"] == 3 and s["ways"] == 2 and s["nodes"] == 1
        assert s["relationsSkipped"] == 1 and s["skipped"] == 0
        assert s["editorsCount"] == 2 and s["changesetsCount"] == 2
        assert s["elementVersionsCount"] == 6
        assert s["oldestElement"].startswith("2019-01-01")
        assert s["bbox"] == [-1.0, -1.0, 3.0, 3.0], s["bbox"]
        assert sum(s["editRecencyBands"]) == 3
        assert sum(s["mapperRecencyBands"]) == 2
        assert s["editRecencyBands"][3] == 1
        assert s["qualityMetrics"]["staleElementsCount"] == 1
        assert s["tagCounts"][0] == {"key": "building", "count": 2}, s["tagCounts"]

        # tippecanoe redraws progress on \r; we take the last percentage in a read
        assert PCT_RE.findall("\r  31.0%  12/2098/1352  \r  100.0%  14/8392/5414")[-1] \
            == "100.0"

        assert apply_settings("[out:json][timeout:9];way;out;", 512, 60) == \
            "[out:json][maxsize:512][timeout:60];way;out;"
        assert apply_settings("way;out;", None, 60) == "[out:json][timeout:60];way;out;"
        assert apply_settings("[out:json];way;", None, None) == "[out:json];way;"
        assert html_error(
            "<html><p>copyright boilerplate</p>"
            "<p><b>Error</b>: line 1: Unknown type &quot;this&quot;</p></html>"
        ) == 'Error : line 1: Unknown type "this"'
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        serve()
