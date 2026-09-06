"""The work queue and the serial worker that drives a job through its stages.

One job at a time: the serial queue doubles as contention control on the
box that also serves interactive Overpass queries.
"""
import json
import os
import queue

import config
from bake import stage_bake
from config import RUNNING, log
from convert import convert
from errors import Aborted, StageError
from overpass import classify_error, stage_fetch
from spool import (job_directory, progress_writer, read_job, set_state, sweep)

Q = queue.Queue()


def recover() -> None:
    """Crash recovery: running states go back to queued, then re-enqueue FIFO."""
    pending = []
    for name in os.listdir(config.SPOOL):
        job = read_job(name)
        if not job:
            continue
        if job.get("state") in RUNNING:
            set_state(job, "queued")  # via set_state so stale progress clears
            log("recovered", name)
        if job.get("state") == "queued":
            pending.append(job)
    for job in sorted(pending, key=lambda j: j.get("startedAt") or ""):
        Q.put(job["id"])


def run_job(job_id: str) -> None:
    job = read_job(job_id)
    if not job:
        return
    job_dir = job_directory(job_id)
    raw_path = os.path.join(job_dir, "raw.json")
    try:
        set_state(job, "fetching")
        stage_fetch(job, raw_path)

        set_state(job, "converting")
        stats = convert(raw_path, os.path.join(job_dir, "data.ndjson"),
                        progress_writer(job, "converting"),
                        filter_keys=job.get("filterableTags") or [])
        with open(os.path.join(job_dir, "stats.json"), "w",
                  encoding="utf-8") as f:
            json.dump(stats, f)
        job["stats"] = stats
        os.remove(raw_path)  # ~2 GB at metro scale; the nd-geojson supersedes it
        if not stats["features"]:
            # fail here with the real cause; tippecanoe would exit nonzero
            # with "Did not read any valid geometries" two stages later
            raise StageError("query matched no elements")

        set_state(job, "baking")
        stage_bake(job, job_dir)
        set_state(job, "done")
    except Aborted:
        log("job", job_id, "aborted (deleted)")
    except StageError as error:
        fail(job, str(error))
    except Exception as error:
        fail(job, "%s: %s" % (type(error).__name__, error))


def fail(job: dict, error: str) -> None:
    # keep the run's own dict: a re-read would adopt the on-disk record's
    # generation and stomp a re-POSTed job (write_job couldn't tell them apart)
    job["error"] = error
    kind = classify_error(error)
    if kind:
        job["errorKind"] = kind
    try:
        set_state(job, "failed")
    except Aborted:
        pass
    log("job", job["id"], "failed:", error[:200])


def worker() -> None:
    while True:
        job_id = Q.get()
        try:
            run_job(job_id)
            sweep()
        except Exception as error:
            # a sweep()/fail() error must not kill the only worker thread --
            # the HTTP surface would keep queueing jobs nobody runs
            log("worker error on", job_id, "-",
                "%s: %s" % (type(error).__name__, error))
        finally:
            Q.task_done()
