"""Durable queue for memory adds.

Every add is written to ``memory_add_jobs`` with its messages before the caller
gets an answer, and a worker thread runs the extraction from there. A failed
add is retried with a growing delay instead of being dropped, and a job that
was running when the process stopped is picked up again after its lease runs
out. A finished job is deleted, so its messages do not stay on disk.

Callers poll the platform event (``platform_compat_events``) as before: it
stays PROCESSING while the job waits for a retry and turns SUCCEEDED, or FAILED
once retries run out. A FAILED job keeps its row and messages for a replay.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from db import Base, SessionLocal
from server_state import get_memory_instance
from sqlalchemy import DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

logger = logging.getLogger(__name__)

LEASE = timedelta(minutes=int(os.environ.get("MEM0_ADD_LEASE_MINUTES", "15")))
MAX_ATTEMPTS = int(os.environ.get("MEM0_ADD_MAX_ATTEMPTS", "60"))
IDLE_POLL_SECONDS = float(os.environ.get("MEM0_ADD_POLL_SECONDS", "2"))
# Delay before attempt n+1; the last value repeats, so 60 attempts span about two days
RETRY_DELAYS = (30, 120, 600, 1800, 3600)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AddJob(Base):
    __tablename__ = "memory_add_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# Called with (job_id, status, results, error) when a job finishes, keyed by source
_finishers: Dict[str, Callable[[str, str, List[Any], str], None]] = {}


def on_finish(source: str, callback: Callable[[str, str, List[Any], str], None]) -> None:
    _finishers[source] = callback


def enqueue(source: str, messages: List[Any], add_kwargs: Dict[str, Any], job_id: Optional[str] = None) -> str:
    """Store the add durably and return its id; the caller can answer right away."""
    job_id = job_id or uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(
            AddJob(
                id=job_id,
                source=source,
                payload_json=json.dumps({"messages": messages, "kwargs": add_kwargs}, default=str),
            )
        )
        db.commit()
    _wake.set()
    return job_id


def _claim() -> Optional[AddJob]:
    # SKIP LOCKED lets several workers or processes share the table safely
    now = _utcnow()
    with SessionLocal() as db:
        row = db.execute(
            text(
                """
                UPDATE memory_add_jobs SET status = 'RUNNING', attempts = attempts + 1,
                       locked_until = :lease_end, updated_at = :now
                WHERE id = (
                    SELECT id FROM memory_add_jobs
                    WHERE (status = 'PENDING' AND next_attempt_at <= :now)
                       OR (status = 'RUNNING' AND locked_until < :now)
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1)
                RETURNING id
                """
            ),
            {"now": now, "lease_end": now + LEASE},
        ).first()
        db.commit()
        return db.get(AddJob, row[0]) if row else None


def _finish(job: AddJob, status: str, results: List[Any], error: str) -> None:
    callback = _finishers.get(job.source)
    if callback:
        callback(job.id, status, results, error)


def _run(job: AddJob) -> None:
    payload = json.loads(job.payload_json)
    try:
        response = (
            get_memory_instance().add(messages=payload["messages"], **payload["kwargs"]) if payload["messages"] else {}
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:1000]
        with SessionLocal() as db:
            row = db.get(AddJob, job.id)
            row.last_error = error
            row.locked_until = None
            row.updated_at = _utcnow()
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "FAILED"
                logger.error("memory add %s failed for good after %d attempts: %s", job.id, row.attempts, error)
            else:
                row.status = "PENDING"
                row.next_attempt_at = _utcnow() + timedelta(
                    seconds=RETRY_DELAYS[min(row.attempts, len(RETRY_DELAYS)) - 1]
                )
                logger.warning(
                    "memory add %s attempt %d failed, retrying at %s: %s",
                    job.id,
                    row.attempts,
                    row.next_attempt_at.isoformat(),
                    error,
                )
            failed = row.status == "FAILED"
            db.commit()
        if failed:
            _finish(job, "FAILED", [], error)
        return

    results = response.get("results", []) if isinstance(response, dict) else []
    _finish(job, "SUCCEEDED", results, "")
    with SessionLocal() as db:
        db.query(AddJob).filter(AddJob.id == job.id).delete()
        db.commit()


_wake = threading.Event()
_started = False
_start_lock = threading.Lock()


def _loop() -> None:
    while True:
        try:
            job = _claim()
        except Exception:
            logger.exception("memory add queue: claiming a job failed")
            job = None
        if job is None:
            _wake.wait(IDLE_POLL_SECONDS)
            _wake.clear()
            continue
        try:
            _run(job)
        except Exception:
            # The job keeps its lease and is retried once the lease runs out
            logger.exception("memory add queue: job %s crashed the worker loop", job.id)
            time.sleep(IDLE_POLL_SECONDS)


def start_worker() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="memory-add-queue", daemon=True).start()
