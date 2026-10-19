"""Database engine and the pipeline-run bookkeeping every job goes through."""
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import DATABASE_URL, ROOT


@lru_cache
def engine() -> Engine:
    return create_engine(DATABASE_URL, pool_pre_ping=True, future=True)


def apply_schema() -> None:
    sql = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    with engine().begin() as conn:
        conn.exec_driver_sql(sql)


class RunStats:
    """Counters a job fills in; written to pipeline_runs when the job ends."""

    def __init__(self) -> None:
        self.rows_in = 0
        self.rows_out = 0
        self.notes: dict = {}


@contextmanager
def pipeline_run(job_name: str):
    """Record start/end/status/row counts of a job in pipeline_runs.

    Failures are logged and re-raised so the caller decides whether the
    pipeline continues.
    """
    with engine().begin() as conn:
        run_id = conn.execute(
            text("insert into pipeline_runs (job_name, status) values (:j, 'running') returning run_id"),
            {"j": job_name},
        ).scalar_one()
    stats = RunStats()
    try:
        yield stats
    except Exception as exc:
        _finish(run_id, "failed", stats, f"{type(exc).__name__}: {exc}"[:2000])
        raise
    else:
        _finish(run_id, "success", stats, None)


def record_skip(job_name: str, reason: str) -> None:
    import json

    with engine().begin() as conn:
        conn.execute(
            text("""insert into pipeline_runs (job_name, status, completed_at, notes)
                    values (:j, 'skipped', now(), cast(:n as jsonb))"""),
            {"j": job_name, "n": json.dumps({"reason": reason})},
        )


def _finish(run_id: int, status: str, stats: RunStats, error: str | None) -> None:
    import json

    with engine().begin() as conn:
        conn.execute(
            text(
                """update pipeline_runs
                   set status = :s, completed_at = now(), rows_in = :ri, rows_out = :ro,
                       notes = cast(:n as jsonb), error = :e
                   where run_id = :id"""
            ),
            {"s": status, "ri": stats.rows_in, "ro": stats.rows_out,
             "n": json.dumps(stats.notes, default=str), "e": error, "id": run_id},
        )
