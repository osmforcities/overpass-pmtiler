"""Job records on disk: one directory per job, one job.json per directory.

No database -- the spool directory is the whole persistence layer. Writes go
through write_job (atomic tmp + rename), which doubles as the abort
checkpoint (see errors.Aborted).
"""
import json
import os
import shutil
import time
from collections.abc import Callable, Iterator

import config
from config import DAY, log, now_iso
from errors import Aborted


def job_directory(job_id: str) -> str:
    return os.path.join(config.SPOOL, job_id)


def read_job(job_id: str) -> dict | None:
    try:
        with open(os.path.join(job_directory(job_id), "job.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:  # absent is normal (idempotency checks, 404s)
        return None
    except (OSError, ValueError) as error:
        # corrupt is not: without this line the job silently vanishes from
        # /jobs, recover() skips it forever and sweep waits MAX_AGE_DAYS
        log("unreadable job.json for", job_id, "-",
            "%s: %s" % (type(error).__name__, error))
        return None


def list_jobs() -> Iterator[dict]:
    """Every readable job record in the spool, unordered. Callers must not
    need to know the spool is a directory of directories."""
    return filter(None, map(read_job, os.listdir(config.SPOOL)))


def write_job(job: dict) -> None:
    # tmp + os.replace makes every read see a whole record: the reason
    # job.json needs no lock across threads. Never write it in place.
    directory = job_directory(job["id"])
    if not os.path.isdir(directory):
        raise Aborted(job["id"])
    current = read_job(job["id"])  # startedAt is the run's generation
    if current and current.get("startedAt") != job.get("startedAt"):
        raise Aborted(job["id"])  # dir was deleted and recreated under us
    job["updatedAt"] = now_iso()
    path = os.path.join(directory, "job.json")
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(job, f)
    os.replace(temp_path, path)


def set_state(job: dict, state: str) -> None:
    job["state"] = state
    job.pop("progress", None)
    write_job(job)
    log("job", job["id"], "->", state)


def progress_writer(job: dict, stage: str,
                    interval: float = 1.0) -> Callable[..., None]:
    """Throttled progress into job.json -- callers poll state, nothing is
    pushed. Each write is also an abort checkpoint (see Aborted)."""
    last_write = [0.0]

    def tick(**fields) -> None:
        if time.time() - last_write[0] < interval:
            return
        last_write[0] = time.time()
        job["progress"] = dict(stage=stage, **fields)
        write_job(job)

    return tick


def sweep() -> None:
    """Delete job dirs older than MAX_AGE_DAYS. Runs at startup and after
    each job rather than on a timer -- an idle service never sweeps."""
    cutoff = time.time() - config.MAX_AGE_DAYS * DAY
    for name in os.listdir(config.SPOOL):
        path = os.path.join(config.SPOOL, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                log("swept", name)
        except OSError:
            pass
