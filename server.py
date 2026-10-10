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
import time

import config
from config import (HERE, ID_RE, MAX_AGE_BANDS, MAX_FILTER_KEYS, MAX_MAXSIZE, MAX_QUERY,
                    MAX_TIMEOUT, OUTPUTS, log, now_iso)
from runner import Q, recover, worker
from spool import job_directory, list_jobs, read_job, sweep, write_job

CREATE_LOCK = threading.Lock()
STARTED_AT = time.time()

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def spool_bytes() -> int:
    total = 0
    for root, _, files in os.walk(config.SPOOL):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pmtiler/0.1"
    protocol_version = "HTTP/1.1"

    def send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def parse_job_path(self) -> tuple:
        parts = self.path.split("?")[0].strip("/").split("/")
        if not parts or parts[0] != "jobs":
            return None, None
        job_id = parts[1] if len(parts) > 1 else None
        if job_id is not None and not ID_RE.match(job_id):
            return None, None
        return job_id, parts[2] if len(parts) > 2 else None

    def do_POST(self) -> None:
        if self.path.split("?")[0].rstrip("/") != "/jobs":
            return self.send_error_json(404, "not found")
        length = self.headers.get("Content-Length") or "0"
        # isdecimal, not int(): int() takes "-1", and rfile.read(-1) blocks
        # until the client closes
        if not length.isdecimal():
            return self.send_error_json(400, "invalid Content-Length")
        if int(length) > MAX_QUERY + 4096:
            return self.send_error_json(413, "body too large")
        try:
            request = json.loads(self.rfile.read(int(length)) or b"{}")
        except ValueError:
            return self.send_error_json(400, "invalid json")
        if not isinstance(request, dict):
            return self.send_error_json(400, "body must be a JSON object")

        job_id = request.get("id")
        query = request.get("query")
        if not isinstance(job_id, str) or not ID_RE.match(job_id):
            return self.send_error_json(400,
                                        "id must match [A-Za-z0-9._-]{1,128}")
        if not isinstance(query, str) or not query.strip():
            return self.send_error_json(400, "query is required")
        if len(query) > MAX_QUERY:
            return self.send_error_json(400,
                                        "query exceeds %d bytes" % MAX_QUERY)
        maxsize = request.get("maxsize")
        timeout = request.get("timeout")
        if any(value is not None
               and (not isinstance(value, int) or isinstance(value, bool))
               for value in (maxsize, timeout)):
            return self.send_error_json(400,
                                        "maxsize and timeout must be integers")
        if maxsize is not None and not 0 < maxsize <= MAX_MAXSIZE:
            return self.send_error_json(400, "maxsize out of range")
        if timeout is not None and not 0 < timeout <= MAX_TIMEOUT:
            return self.send_error_json(400, "timeout out of range")
        filterable_tags = request.get("filterableTags")
        if filterable_tags is not None and not (
                isinstance(filterable_tags, list)
                and len(filterable_tags) <= MAX_FILTER_KEYS
                and all(isinstance(key, str) and 0 < len(key) <= 255
                        for key in filterable_tags)):
            return self.send_error_json(
                400, "filterableTags must be a list of up to %d "
                "tag keys" % MAX_FILTER_KEYS)
        keep_meta = request.get("keepMeta")
        if keep_meta is not None and not isinstance(keep_meta, bool):
            return self.send_error_json(400, "keepMeta must be a boolean")
        age_bands_days = request.get("ageBandsDays")
        if age_bands_days is not None and not (
                isinstance(age_bands_days, list)
                and 0 < len(age_bands_days) <= MAX_AGE_BANDS
                and all(isinstance(days, int) and not isinstance(days, bool)
                        and 0 < days <= 36500 for days in age_bands_days)
                and all(a < b for a, b in
                        zip(age_bands_days, age_bands_days[1:]))):
            return self.send_error_json(
                400, "ageBandsDays must be an ascending list of up to %d "
                "day cutoffs" % MAX_AGE_BANDS)

        with CREATE_LOCK:
            existing = read_job(job_id)
            if existing:  # idempotent resubmit: report state, never re-run
                return self.send_json(200, {"id": job_id,
                                            "state": existing["state"]})
            os.makedirs(job_directory(job_id), exist_ok=True)
            write_job({"id": job_id, "query": query, "maxsize": maxsize,
                       "timeout": timeout, "filterableTags": filterable_tags,
                       "keepMeta": bool(keep_meta),
                       "ageBandsDays": age_bands_days,
                       "state": "queued",
                       "startedAt": now_iso(), "error": None, "stats": None})
            Q.put(job_id)
        log("job", job_id, "queued")
        self.send_json(202, {"id": job_id, "state": "queued"})

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/viewer.html"):  # visual inspection, same-origin
            return self.send_file(os.path.join(HERE, "viewer.html"),
                                  "text/html")
        if path == "/status":
            states = {}
            for job in list_jobs():
                state = job.get("state") or "unknown"
                states[state] = states.get(state, 0) + 1
            disk = shutil.disk_usage(config.SPOOL)
            return self.send_json(200, {
                "version": self.server_version,
                "uptimeSeconds": int(time.time() - STARTED_AT),
                "queueDepth": Q.qsize(),
                "jobStates": states,
                "spoolBytes": spool_bytes(),
                "diskFreeBytes": disk.free,
            })
        if path == "/jobs":
            # .get() throughout: one malformed job.json must not 500 the list.
            # Optional fields are omitted when absent, never null -- the same
            # shape as GET /jobs/:id and the API.md contract.
            rows = []
            for job in list_jobs():
                row = {"id": job.get("id"), "state": job.get("state"),
                       "startedAt": job.get("startedAt")}
                if job.get("progress"):
                    row["progress"] = job["progress"]
                if job.get("errorKind"):
                    row["errorKind"] = job["errorKind"]
                rows.append(row)
            return self.send_json(200, sorted(
                rows, key=lambda row: row["startedAt"] or "", reverse=True))
        job_id, output_name = self.parse_job_path()
        if not job_id:
            return self.send_error_json(404, "not found")
        job = read_job(job_id)
        if not job:
            return self.send_error_json(404, "no such job")
        if output_name is None:
            view = {"id": job_id, "state": job["state"],
                    "startedAt": job["startedAt"]}
            if job.get("progress"):
                view["progress"] = job["progress"]
            if job.get("error"):
                view["error"] = job["error"]
            if job.get("errorKind"):
                view["errorKind"] = job["errorKind"]
            if job.get("stats"):
                view["stats"] = job["stats"]
            return self.send_json(200, view)
        if output_name not in OUTPUTS:
            return self.send_error_json(404, "not found")
        path = os.path.join(job_directory(job_id), output_name)
        if not os.path.isfile(path):
            return self.send_error_json(
                404, "%s not ready (state %s)" % (output_name, job["state"]))
        self.send_file(path, OUTPUTS[output_name])

    do_HEAD = do_GET

    def do_DELETE(self) -> None:
        job_id, output_name = self.parse_job_path()
        if not job_id or output_name:
            return self.send_error_json(404, "not found")
        if not os.path.isdir(job_directory(job_id)):
            return self.send_error_json(404, "no such job")
        shutil.rmtree(job_directory(job_id), ignore_errors=True)
        log("job", job_id, "deleted")
        self.send_json(200, {"id": job_id, "state": "deleted"})

    def send_file(self, path: str, content_type: str) -> None:
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        # ponytail: single range only; a multi-range request gets the full body.
        match = RANGE_RE.fullmatch((self.headers.get("Range") or "").strip())
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            else:
                start = max(0, size - int(match.group(2)))
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
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
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


def serve() -> None:
    os.makedirs(config.SPOOL, exist_ok=True)
    sweep()
    recover()
    threading.Thread(target=worker, daemon=True).start()
    httpd = Server((config.BIND, config.PORT), Handler)
    log("pmtiler on http://%s:%d spool=%s overpass=%s"
        % (config.BIND, httpd.server_address[1], config.SPOOL, config.OVERPASS))
    httpd.serve_forever()
