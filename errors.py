"""The two exceptions every stage shares."""


class StageError(Exception):
    """Carries user-visible error text into the job's `failed` state."""


class Aborted(Exception):
    """The abort seam, in one place. DELETE removes the job dir; the running
    job notices on its next write_job (progress tick or state change), which
    raises this and unwinds the stage -- run_job exits quietly. A re-POST of
    a deleted id recreates the record with a fresh startedAt, so a stale
    run's write_job aborts on the generation mismatch instead of writing
    into the new job's directory. Stages abort only at write_job calls;
    stage_bake additionally kills its tippecanoe subprocess on unwind."""
