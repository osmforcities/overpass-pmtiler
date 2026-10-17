"""The bake stage: tippecanoe over the nd-geojson, then validate the archive."""
import os
import re
import subprocess

import config
from config import log
from errors import StageError
from spool import progress_writer


def validate_pmtiles(path: str) -> None:
    """PMTiles v3 header: magic, version, and a nonzero addressed-tile count."""
    with open(path, "rb") as f:
        header = f.read(127)
    if len(header) < 127 or header[:7] != b"PMTiles" or header[7] != 3:
        raise StageError("bake produced an unreadable pmtiles archive")
    if int.from_bytes(header[72:80], "little") == 0:
        raise StageError("bake produced an archive with no tiles")


PROGRESS_RE = re.compile(r'^\{"progress":\s*([\d.]+)\}$')
# -zg needs two distinct feature locations: a lone point (or points stacked on
# one spot) cannot guess, so that input rebakes at the zoom a dense small area
# would get. tippecanoe decides, not a count: one polygon guesses fine.
NO_GUESS = "Can't guess maxzoom (-zg)"
FIXED_MAXZOOM = "-z14"


def stage_bake(job: dict, job_dir: str) -> None:
    try:
        run_tippecanoe(job, job_dir, "-zg")
    except StageError as error:
        if NO_GUESS not in str(error):
            raise
        run_tippecanoe(job, job_dir, FIXED_MAXZOOM)
    archive_path = os.path.join(job_dir, "output.pmtiles")
    validate_pmtiles(archive_path)
    log("baked", job["id"], os.path.getsize(archive_path), "bytes")


def run_tippecanoe(job: dict, job_dir: str, maxzoom: str) -> None:
    archive_path = os.path.join(job_dir, "output.pmtiles")
    command = ["nice", "-n", config.NICE, "tippecanoe", "-f", maxzoom,
               "--drop-densest-as-needed", "-P", "-l", "features",
               # --json-progress replaces the \r-redrawn bar with one line per
               # tick, and leaves real errors on stderr untouched
               "--json-progress", "--progress-interval=1"]
    if job.get("keepMeta"):
        # Editor metadata is an nd-geojson-only affordance: keep the tiles
        # byte-identical to a non-keepMeta bake
        for meta_key in ("@user", "@uid", "@timestamp", "@version",
                         "@changeset"):
            command += ["-x", meta_key]
    command += ["-o", archive_path, os.path.join(job_dir, "data.ndjson")]
    # tmp files stay inside the job dir, never /tmp
    process = subprocess.Popen(command, cwd=job_dir,
                               env=dict(os.environ, TMPDIR=job_dir),
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
    tick = progress_writer(job, "baking")
    errors = []
    try:
        for line in process.stderr:  # tippecanoe throttles: every line is used
            match = PROGRESS_RE.match(line.strip())
            if match:
                tick(pct=float(match.group(1)))
            elif line.strip():
                errors.append(line.rstrip())
                del errors[:-20]
        if process.wait() != 0:
            raise StageError(
                "tippecanoe: " + ("\n".join(errors)[-2000:] or "failed"))
    finally:
        if process.poll() is None:  # abort mid-bake: don't orphan the subprocess
            process.kill()
            process.wait()
