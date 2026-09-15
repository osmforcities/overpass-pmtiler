"""The fetch stage: stream one Overpass query result to disk.

Also owns the query settings merge and the two ways Overpass reports
errors -- an HTML error page (under any status, 200 included), and a
"remark" key in an otherwise-200 body.
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
# The ways Overpass says "this query is over the ceiling": the areas
# dispatcher refusing to evaluate (total_available_space exhausted, surfacing
# as a Dispatcher_Client protocol error) and the runtime exhausting its
# [maxsize:] budget. Both are permanent for a given query -- callers should
# not retry, unlike unclassified failures.
TOO_LARGE_RE = re.compile(
    r"out of memory|total_available_space|Dispatcher_Client", re.I)
# anchored to the pretty-printer's top-level indent: an element's remark=* TAG
# sits deeper and must not fail a successful fetch
REMARK_RE = re.compile(r'^  "remark"\s*:\s*"((?:[^"\\]|\\.)*)"', re.M)
HTML_TAG_RE = re.compile(r"<[^>]+>")


def classify_error(text: str) -> str | None:
    """Machine-readable failure kind for the job record, or None when
    unclassified (a caller may treat those as transient and retry)."""
    return "too_large" if TOO_LARGE_RE.search(text) else None


def apply_settings(query: str, maxsize: int | None, timeout: int | None) -> str:
    """Merge [maxsize:]/[timeout:] into the query's leading settings statement."""
    stripped = query.strip()
    extra = ""
    if maxsize:
        extra += "[maxsize:%d]" % maxsize
    if timeout:
        extra += "[timeout:%d]" % timeout
    if not extra:
        return stripped
    settings_end = stripped.find(";")
    if stripped.startswith("[") and settings_end != -1:
        return (SETTING_RE.sub("", stripped[:settings_end]) + extra
                + stripped[settings_end:])
    return "[out:json]" + extra + ";" + stripped


def html_error(body: str) -> str:
    """Overpass returns its error text inside an HTML page, after boilerplate."""
    text = re.sub(r"\s+", " ", html.unescape(HTML_TAG_RE.sub(" ", body))).strip()
    match = re.search(r"Error\s*:", text)  # stripping leaves "<b>Error</b>:" spaced
    if match:
        text = text[match.start():]
    return text[:2000] or "overpass returned an error"


def head_error(path: str) -> str | None:
    """Overpass sometimes answers 200 with its HTML error page instead of data
    (the areas dispatcher refusing, for one). [out:json] output never starts
    with "<", so markup carrying an "Error:" line is an error page -- caught
    here, the real text reaches the job record and classifies, instead of
    surfacing a stage later as a converter format complaint.

    Markup without that line is not an error: a caller who asked for [out:xml]
    gets the converter's format complaint, which names the real problem better
    than the copyright boilerplate html_error would return for it."""
    with open(path, "rb") as f:
        head = f.read(8192)  # the page is a few hundred bytes; html_error caps at 2000
    if head.lstrip()[:1] != b"<":
        return None
    text = html_error(head.decode("utf-8", "replace"))
    return text if text.startswith("Error") else None  # html_error anchors there


def tail_remark(path: str) -> str | None:
    """Overpass reports runtime errors as a "remark" key in an otherwise-200 body."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - 8192))
        tail = f.read().decode("utf-8", "replace")
    match = REMARK_RE.search(tail)
    return match.group(1) if match else None


def stage_fetch(job: dict, path: str) -> None:
    query = apply_settings(job["query"], job.get("maxsize"), job.get("timeout"))
    form_body = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(
        config.OVERPASS, data=form_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    ssl_context = ssl._create_unverified_context() if config.INSECURE else None
    tick = progress_writer(job, "fetching")
    try:
        with urllib.request.urlopen(
                request, context=ssl_context,
                timeout=(job.get("timeout") or 180) + 60) as response, \
                open(path, "wb") as out:
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                received += len(chunk)
                # no total: Overpass answers chunked, there is no Content-Length
                tick(bytes=received)
    except urllib.error.HTTPError as error:
        raise StageError(html_error(error.read().decode("utf-8", "replace")))
    except urllib.error.URLError as error:
        raise StageError("overpass unreachable: %s" % (error.reason,))
    except TimeoutError:
        raise StageError("overpass read timed out after %ds"
                         % ((job.get("timeout") or 180) + 60))
    error_text = head_error(path)
    if error_text:
        raise StageError(error_text)
    remark = tail_remark(path)
    if remark:
        raise StageError(remark)
    log("fetched", job["id"], os.path.getsize(path), "bytes")
