"""The fetch stage: stream one Overpass query result to disk.

Also owns the query settings merge and the two ways Overpass reports
errors -- an HTML error page, and a "remark" key in an otherwise-200 body.
"""
import html
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

import config
from config import log
from errors import StageError
from spool import progress_writer

SETTING_RE = re.compile(r"\[\s*(?:maxsize|timeout)\s*:\s*\d+\s*\]", re.I)
# anchored to the pretty-printer's top-level indent: an element's remark=* TAG
# sits deeper and must not fail a successful fetch
REMARK_RE = re.compile(r'^  "remark"\s*:\s*"((?:[^"\\]|\\.)*)"', re.M)
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
        config.OVERPASS, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    ctx = ssl._create_unverified_context() if config.INSECURE else None
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
    except TimeoutError:
        raise StageError("overpass read timed out after %ds"
                         % ((job.get("timeout") or 180) + 60))
    remark = tail_remark(path)
    if remark:
        raise StageError(remark)
    log("fetched", job["id"], os.path.getsize(path), "bytes")
