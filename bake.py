"""The bake stage: tippecanoe over the nd-geojson, then validate the archive.

An archive reaching z14 is a split bake: z9-z13 rebaked with only the kept
keys (filterableTags, _ts, @id), z14+ rebaked with every tag, both joined
with tile-join after the first bake's own z0-z8. A rebaked zoom whose largest
tile is over the tile cap is thinned by one rate for the whole zoom, so
density stays even across tiles, and no zoom keeps a bigger share of the
features than the zoom above it. An over-cap top zoom gains deeper zooms
until one fits, up to z16; a z16 still over the cap is thinned too. A zoom
that one rate cannot bring under the cap falls back to tippecanoe's own
per-tile dropping, which is uneven.
"""
import glob
import json
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
# With only the kept keys and coarser geometry, city zooms of a dense dataset
# fit many more features per tile. Full tags stay at z14+.
SLIM_MIN_ZOOM = 9
SLIM_MAX_ZOOM = 13
ARCHIVE = "output.pmtiles"
FULL, LOW = "full.pmtiles", "low.pmtiles"
# tippecanoe's default compressed tile limit
TILE_CAP_BYTES = 500_000
MAX_TOP_ZOOM = 16
UNCAPPED = ["--no-tile-size-limit", "--no-feature-limit"]
THIN_ATTEMPTS = 4
THIN_AIM_OF_CAP = 0.8
THIN_ACCEPT_FLOOR_OF_CAP = 0.6


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
        high, high_share = even_density_parts(
            job, job_dir, "high", SLIM_MAX_ZOOM + 1, top_zoom, [], 1.0,
            extend=True)
        slim, _ = even_density_parts(
            job, job_dir, "slim", SLIM_MIN_ZOOM, SLIM_MAX_ZOOM,
            ["--no-tiny-polygon-reduction", "--low-detail=10",
             "--full-detail=10", *kept_keys], high_share)
        os.replace(os.path.join(job_dir, ARCHIVE), os.path.join(job_dir, FULL))
        run_tile_join(job, job_dir,
                      ["-z%d" % (SLIM_MIN_ZOOM - 1), "-o", LOW, FULL])
        run_tile_join(job, job_dir, ["-o", ARCHIVE, LOW, *slim, *high])
    finally:
        for part in glob.glob(os.path.join(glob.escape(job_dir), "*.pmtiles")):
            if os.path.basename(part) != ARCHIVE:
                os.remove(part)


def even_density_parts(job: dict, job_dir: str, prefix: str, min_zoom: int,
                       max_zoom: int, flags: list, share_ceiling: float,
                       extend: bool = False) -> tuple:
    """Return the part files for min_zoom..max_zoom (plus the zooms extend
    adds above it) and the share of features the lowest zoom keeps."""
    def bake(name, low, high, share=1.0, capped=False, extra=()):
        # -B one zoom above makes -r keep about `share` of the features
        run_tippecanoe(job, job_dir, name,
                       ["-Z%d" % low, "-z%d" % high, "-B%d" % (high + 1),
                        "-r%.4f" % (1 / share), "--drop-polygons",
                        "--drop-lines", *flags, *extra,
                        *([] if capped else UNCAPPED)])
        return largest_tile_bytes_by_zoom(job, job_dir, name)

    def bake_alone(zoom, share=1.0, capped=False):
        # a zoom baked alone is its own max zoom, where simplification
        # collapses small polygons the probe kept at that zoom
        return bake(zoom_part(zoom), zoom, zoom, share, capped,
                    ["--no-simplification"]).get(zoom, 0)

    def zoom_part(zoom):
        return "%s-z%d.pmtiles" % (prefix, zoom)

    def thin(zoom, size, ceiling):
        # tile size is not linear in the share, so each attempt rescales
        share = 1.0
        for _ in range(THIN_ATTEMPTS):
            share = (min(ceiling, share * THIN_AIM_OF_CAP * TILE_CAP_BYTES / size)
                     if size else ceiling)
            size = bake_alone(zoom, share)
            if size <= TILE_CAP_BYTES and (
                    share == ceiling
                    or size >= THIN_ACCEPT_FLOOR_OF_CAP * TILE_CAP_BYTES):
                return share
        if size > TILE_CAP_BYTES:
            bake_alone(zoom, share, capped=True)
        return share

    probe = prefix + ".pmtiles"
    sizes = bake(probe, min_zoom, max_zoom)
    top_zoom = max_zoom
    while (extend and sizes.get(top_zoom, 0) > TILE_CAP_BYTES
           and top_zoom < MAX_TOP_ZOOM):
        top_zoom += 1
        sizes[top_zoom] = bake_alone(top_zoom)

    parts = []
    lowest_whole_zoom = max_zoom + 1
    for zoom in range(top_zoom, min_zoom - 1, -1):
        size = sizes.get(zoom, 0)
        if share_ceiling == 1.0 and size <= TILE_CAP_BYTES:
            if zoom <= max_zoom:
                lowest_whole_zoom = zoom
            else:
                parts.append(zoom_part(zoom))
            continue
        share_ceiling = thin(zoom, size, share_ceiling)
        parts.append(zoom_part(zoom))
    if lowest_whole_zoom == min_zoom:
        parts.append(probe)
    elif lowest_whole_zoom <= max_zoom:
        whole = prefix + "-whole.pmtiles"
        run_tile_join(job, job_dir,
                      ["-Z%d" % lowest_whole_zoom, "-o", whole, probe])
        parts.append(whole)
    return parts, share_ceiling


def largest_tile_bytes_by_zoom(job: dict, job_dir: str, name: str) -> dict:
    stats = run_without_progress(job, job_dir, "tippecanoe-decode",
                                 ["--stats", name])
    sizes = {}
    for tile in json.loads(stats):
        sizes[tile["zoom"]] = max(sizes.get(tile["zoom"], 0), tile["bytes"])
    return sizes


def spawn(job_dir: str, command: list,
          stdout: int = subprocess.DEVNULL) -> subprocess.Popen:
    # tmp files stay inside the job dir, never /tmp
    return subprocess.Popen(["nice", "-n", config.NICE, *command], cwd=job_dir,
                            env=dict(os.environ, TMPDIR=job_dir),
                            stdout=stdout, stderr=subprocess.PIPE, text=True)


def run_tile_join(job: dict, job_dir: str, arguments: list) -> None:
    run_without_progress(job, job_dir, "tile-join",
                         ["-f", "-q", "--no-tile-size-limit", *arguments])


def run_without_progress(job: dict, job_dir: str, tool: str,
                         arguments: list) -> str:
    process = spawn(job_dir, [tool, *arguments], stdout=subprocess.PIPE)
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                write_job(job)  # abort checkpoint: the tool reports no progress
    finally:
        if process.poll() is None:  # abort mid-run: don't orphan the tool
            process.kill()
            process.wait()
    if process.returncode != 0:
        raise StageError("%s: %s" % (tool, stderr.strip()[-2000:] or "failed"))
    return stdout


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
