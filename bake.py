"""The bake stage: tippecanoe over the nd-geojson, then validate the archive."""
import os
import re
import subprocess

import config
from config import log
from errors import StageError
from spool import progress_writer


def validate_pmtiles(path):
    """PMTiles v3 header: magic, version, and a nonzero addressed-tile count."""
    with open(path, "rb") as f:
        head = f.read(127)
    if len(head) < 127 or head[:7] != b"PMTiles" or head[7] != 3:
        raise StageError("bake produced an unreadable pmtiles archive")
    if int.from_bytes(head[72:80], "little") == 0:
        raise StageError("bake produced an archive with no tiles")


PROGRESS_RE = re.compile(r'^\{"progress":\s*([\d.]+)\}$')


def stage_bake(job, jd):
    out = os.path.join(jd, "output.pmtiles")
    cmd = ["nice", "-n", config.NICE, "tippecanoe", "-f", "-zg",
           "--drop-densest-as-needed", "-P", "-l", "features",
           # --json-progress replaces the \r-redrawn bar with one line per tick,
           # and leaves real errors on stderr untouched
           "--json-progress", "--progress-interval=1",
           "-o", out, os.path.join(jd, "data.ndjson")]
    # tmp files stay inside the job dir, never /tmp
    p = subprocess.Popen(cmd, cwd=jd, env=dict(os.environ, TMPDIR=jd),
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True)
    tick = progress_writer(job, "baking")
    errors = []
    try:
        for line in p.stderr:  # tippecanoe throttles, so every line is used
            m = PROGRESS_RE.match(line.strip())
            if m:
                tick(pct=float(m.group(1)))
            elif line.strip():
                errors.append(line.rstrip())
                del errors[:-20]
        if p.wait() != 0:
            raise StageError(
                "tippecanoe: " + ("\n".join(errors)[-2000:] or "failed"))
    finally:
        if p.poll() is None:  # abort mid-bake: don't orphan the subprocess
            p.kill()
            p.wait()
    validate_pmtiles(out)
    log("baked", job["id"], os.path.getsize(out), "bytes")
