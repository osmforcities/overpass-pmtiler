"""Environment configuration, shared constants, and the two logging helpers.

Env-derived values (SPOOL and friends) are read through attribute access --
`import config` then `config.SPOOL` at call time, never `from config import
SPOOL` -- so tests and tools can reassign them at runtime. Pure constants
(limits, bands, content types) are safe to from-import.
"""
import datetime
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SPOOL = os.path.abspath(os.environ.get("PMTILER_SPOOL", "spool"))
OVERPASS = os.environ.get("PMTILER_OVERPASS", "http://localhost/api/interpreter")
PORT = int(os.environ.get("PMTILER_PORT", "8099"))
BIND = os.environ.get("PMTILER_BIND", "127.0.0.1")
INSECURE = os.environ.get("PMTILER_INSECURE") == "1"
NICE = os.environ.get("PMTILER_NICE", "10")
MAX_AGE_DAYS = float(os.environ.get("PMTILER_MAX_AGE_DAYS", "7"))

# (?!\.\.?$): "." and ".." would otherwise pass, and job_directory() joins
# the id onto the spool path -- DELETE /jobs/.. would rmtree the spool's
# parent and POST id ".." would write records there
ID_RE = re.compile(r"^(?!\.\.?$)[A-Za-z0-9._-]{1,128}$")
MAX_QUERY = 64 * 1024
MAX_MAXSIZE = 4 * 1024 ** 3
MAX_TIMEOUT = 3600
RUNNING = ("fetching", "converting", "baking")
OUTPUTS = {
    "output.pmtiles": "application/octet-stream",
    "stats.json": "application/json",
    "data.ndjson": "application/x-ndjson",
}

DAY = 86400
BANDS = (90 * DAY, 365 * DAY, 730 * DAY)  # emitted as stats recencyBandsDays
EARTH_R = 6371008.8  # turf's sphere radius: measures match @turf/length+area
MAX_FILTER_KEYS = 32


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(*parts) -> None:
    print(now_iso(), *parts, flush=True)
