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
from overpass import stage_fetch
from spool import (job_directory as jobdir, progress_writer, read_job,
                   set_state, sweep)

Q = queue.Queue()


def recover():
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
                        progress_writer(job, "converting"),
                        filter_keys=job.get("filterableTags") or [])
        with open(os.path.join(jd, "stats.json"), "w", encoding="utf-8") as f:
            json.dump(stats, f)
        job["stats"] = stats
        os.remove(raw)  # ~2 GB at metro scale; the nd-geojson supersedes it
        if not stats["features"]:
            # fail here with the real cause; tippecanoe would exit nonzero
            # with "Did not read any valid geometries" two stages later
            raise StageError("query matched no elements")

        set_state(job, "baking")
        stage_bake(job, jd)
        set_state(job, "done")
    except Aborted:
        log("job", jid, "aborted (deleted)")
    except StageError as e:
        fail(job, str(e))
    except Exception as e:
        fail(job, "%s: %s" % (type(e).__name__, e))


def fail(job, error):
    # keep the run's own dict: a re-read would adopt the on-disk record's
    # generation and stomp a re-POSTed job (write_job couldn't tell them apart)
    job["error"] = error
    try:
        set_state(job, "failed")
    except Aborted:
        pass
    log("job", job["id"], "failed:", error[:200])


def worker():
    while True:
        jid = Q.get()
        try:
            run_job(jid)
            sweep()
        except Exception as e:
            # a sweep()/fail() error must not kill the only worker thread --
            # the HTTP surface would keep queueing jobs nobody runs
            log("worker error on", jid, "-", "%s: %s" % (type(e).__name__, e))
        finally:
            Q.task_done()
