#!/usr/bin/env python3
"""overpass-pmtiler: turn an Overpass QL query into a PMTiles archive.

Serial job runner (one job at a time) + HTTP API, stdlib only. Job state is
one JSON file per job in a spool dir; no database. See README.md for the
contract, and each module for one concern:

    config.py    env configuration, shared constants, logging helpers
    errors.py    StageError, Aborted (the abort seam)
    spool.py     job records on disk, progress, sweep
    overpass.py  the fetch stage, query settings, Overpass error extraction
    convert.py   the streaming converter, polygon rules, geometry math
    bake.py      the tippecanoe stage, archive validation
    runner.py    work queue, crash recovery, the serial worker
    server.py    the HTTP surface
    selftest.py  converter + spool self-checks

    python3 pmtiler.py             serve
    python3 pmtiler.py --selftest  self-check
"""
import sys

from convert import convert  # noqa: F401  oracle_compare.py uses pmtiler.convert
from selftest import FIXTURE, selftest  # noqa: F401  check.py uses pmtiler.FIXTURE
from server import serve

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        serve()
