"""The bake stage: tippecanoe over the nd-geojson, then validate the archive.

An archive reaching z14 is a split bake: z9-z13 rebaked with only the kept
keys (filterableTags, _ts, @id), z14+ rebaked with every tag and no drop
rate, both joined with tile-join after the first bake's own z0-z8.
"""
import os
import re
import subprocess

import config
from config import log
from errors import StageError
from spool import progress_writer, write_job


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
# The 500 KB tile cap drops most features of a dense dataset at city zooms and
# tippecanoe's default drop rate thins sparse ones; with only the kept keys
# these zooms keep them. Full tags stay at z14+.
SLIM_MIN_ZOOM = 9
SLIM_MAX_ZOOM = 13
ARCHIVE = "output.pmtiles"
FULL, SLIM, LOW, HIGH = ("full.pmtiles", "slim.pmtiles", "low.pmtiles",
                         "high.pmtiles")


def stage_bake(job: dict, job_dir: str) -> None:
    try:
        run_tippecanoe(job, job_dir, ARCHIVE, ["-zg"])
    except StageError as error:
        if NO_GUESS not in str(error):
            raise
        run_tippecanoe(job, job_dir, ARCHIVE, [FIXED_MAXZOOM])
    archive_path = os.path.join(job_dir, ARCHIVE)
    validate_pmtiles(archive_path)
    top_zoom = max_zoom(archive_path)
    if top_zoom > SLIM_MAX_ZOOM:
        split_bake(job, job_dir, top_zoom)
        validate_pmtiles(archive_path)
    log("baked", job["id"], os.path.getsize(archive_path), "bytes")


def max_zoom(path: str) -> int:
    with open(path, "rb") as f:
        return f.read(127)[101]


def split_bake(job: dict, job_dir: str, top_zoom: int) -> None:
    kept_keys = []
    for key in [*(job.get("filterableTags") or []), "_ts", "@id"]:
        kept_keys += ["-y", key]
    try:
        run_tippecanoe(job, job_dir, SLIM,
                       ["-Z%d" % SLIM_MIN_ZOOM, "-z%d" % SLIM_MAX_ZOOM, "-r1",
                        "--no-tiny-polygon-reduction", "--low-detail=10",
                        "--full-detail=10", *kept_keys])
        # -r1 here too: below a z15+ top zoom, z14 would otherwise hold fewer
        # features than the slim z13 above it
        run_tippecanoe(job, job_dir, HIGH,
                       ["-Z%d" % (SLIM_MAX_ZOOM + 1), "-z%d" % top_zoom, "-r1"])
        os.replace(os.path.join(job_dir, ARCHIVE), os.path.join(job_dir, FULL))
        run_tile_join(job, job_dir,
                      ["-z%d" % (SLIM_MIN_ZOOM - 1), "-o", LOW, FULL])
        run_tile_join(job, job_dir, ["-o", ARCHIVE, LOW, SLIM, HIGH])
    finally:
        for part in (FULL, SLIM, LOW, HIGH):
            try:
                os.remove(os.path.join(job_dir, part))
            except FileNotFoundError:
                pass


def spawn(job_dir: str, command: list) -> subprocess.Popen:
    # tmp files stay inside the job dir, never /tmp
    return subprocess.Popen(["nice", "-n", config.NICE, *command], cwd=job_dir,
                            env=dict(os.environ, TMPDIR=job_dir),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)


def run_tile_join(job: dict, job_dir: str, arguments: list) -> None:
    process = spawn(job_dir, ["tile-join", "-f", "-q", "--no-tile-size-limit",
                              *arguments])
    try:
        while True:
            try:
                _, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                write_job(job)  # abort checkpoint: tile-join reports no progress
    finally:
        if process.poll() is None:  # abort mid-join: don't orphan tile-join
            process.kill()
            process.wait()
    if process.returncode != 0:
        raise StageError("tile-join: " + (stderr.strip()[-2000:] or "failed"))


def run_tippecanoe(job: dict, job_dir: str, output: str,
                   arguments: list) -> None:
    command = ["tippecanoe", "-f", *arguments,
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
    command += ["-o", os.path.join(job_dir, output),
                os.path.join(job_dir, "data.ndjson")]
    process = spawn(job_dir, command)
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
