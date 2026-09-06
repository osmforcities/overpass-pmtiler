"""Job records on disk: one directory per job, one job.json per directory.

No database -- the spool directory is the whole persistence layer. Writes go
through write_job (atomic tmp + rename), which doubles as the abort
checkpoint (see errors.Aborted).
"""
import json
import os
import shutil
import time

import config
from config import DAY, log, now_iso
from errors import Aborted


def jobdir(jid):
    return os.path.join(config.SPOOL, jid)


def read_job(jid):
    try:
        with open(os.path.join(jobdir(jid), "job.json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:  # absent is normal (idempotency checks, 404s)
        return None
    except (OSError, ValueError) as e:
        # corrupt is not: without this line the job silently vanishes from
        # /jobs, recover() skips it forever and sweep waits MAX_AGE_DAYS
        log("unreadable job.json for", jid, "-", "%s: %s" % (type(e).__name__, e))
        return None


def write_job(job):
    # tmp + os.replace makes every read see a whole record: the reason
    # job.json needs no lock across threads. Never write it in place.
    jd = jobdir(job["id"])
    if not os.path.isdir(jd):
        raise Aborted(job["id"])
    current = read_job(job["id"])  # startedAt is the run's generation
    if current and current.get("startedAt") != job.get("startedAt"):
        raise Aborted(job["id"])  # dir was deleted and recreated under us
    job["updatedAt"] = now_iso()
    path = os.path.join(jd, "job.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f)
    os.replace(tmp, path)


def set_state(job, state):
    job["state"] = state
    job.pop("progress", None)
    write_job(job)
    log("job", job["id"], "->", state)


def progress_writer(job, stage, interval=1.0):
    """Throttled progress into job.json -- callers poll state, nothing is
    pushed. Each write is also an abort checkpoint (see Aborted)."""
    last = [0.0]

    def tick(**fields):
        if time.time() - last[0] < interval:
            return
        last[0] = time.time()
        job["progress"] = dict(stage=stage, **fields)
        write_job(job)

    return tick


def sweep():
    cutoff = time.time() - config.MAX_AGE_DAYS * DAY
    for name in os.listdir(config.SPOOL):
        path = os.path.join(config.SPOOL, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                log("swept", name)
        except OSError:
            pass
