"""The HTTP surface: job create/inspect/outputs/delete, plus serve().

No auth by design -- bind to localhost and front with a reverse proxy
(see README "Private by design").
"""
import http.server
import json
import os
import re
import shutil
import threading

import config
from config import (HERE, ID_RE, MAX_FILTER_KEYS, MAX_MAXSIZE, MAX_QUERY,
                    MAX_TIMEOUT, OUTPUTS, log, now_iso)
from runner import Q, recover, worker
from spool import job_directory as jobdir, read_job, sweep, write_job

CREATE_LOCK = threading.Lock()


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
