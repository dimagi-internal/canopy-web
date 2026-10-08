"""Redis-backed job records for long-running on-demand operations.

The store only persists state; the consumer runs the work (a thread, a Celery
task) and reports back with ``complete`` / ``fail``. Records expire after
``ttl_s`` and every write refreshes it.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class Job:
    job_id: str
    operation: str
    status: str  # 'running' | 'completed' | 'failed'
    started_at: str
    completed_at: str | None = None
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    owner: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "job_id": self.job_id,
            "operation": self.operation,
            "status": self.status,
            "started_at": self.started_at,
        }
        if self.owner is not None:
            d["owner"] = self.owner
        if self.completed_at is not None:
            d["completed_at"] = self.completed_at
        if self.result is not None:
            d["result"] = self.result
        if self.error is not None:
            d["error"] = self.error
            if self.error_code:
                d["error_code"] = self.error_code
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        return cls(
            job_id=d["job_id"],
            operation=d["operation"],
            status=d["status"],
            started_at=d["started_at"],
            completed_at=d.get("completed_at"),
            result=d.get("result"),
            error=d.get("error"),
            error_code=d.get("error_code"),
            owner=d.get("owner"),
        )


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


class JobStore:
    """Expects a Redis client created with ``decode_responses=True``."""

    def __init__(self, redis: Any, capability: str, ttl_s: int = 3600, *, prefix: str | None = None):
        self._r = redis
        self._ttl_s = ttl_s
        self._prefix = prefix if prefix is not None else f"ondemand:{capability}:job:"

    def create(self, operation: str, owner: str | None = None) -> Job:
        job = Job(secrets.token_hex(8), operation, "running", _now(), owner=owner)
        self._write(job)
        return job

    def get(self, job_id: str) -> Job | None:
        raw = self._r.get(self._prefix + job_id)
        return Job.from_dict(json.loads(raw)) if raw else None

    def complete(self, job_id: str, result: Any) -> None:
        job = self._get_or_expired(job_id, "completed")
        job.status, job.completed_at = "completed", _now()
        job.result, job.error, job.error_code = result, None, None
        self._write(job)

    def fail(self, job_id: str, error: str, code: str | None = None) -> None:
        job = self._get_or_expired(job_id, "failed")
        job.status, job.completed_at = "failed", _now()
        job.result, job.error, job.error_code = None, error, code
        self._write(job)

    def _get_or_expired(self, job_id: str, status: str) -> Job:
        # A record that expired before the worker finished is re-created so a late
        # poll still sees the outcome instead of a 404.
        return self.get(job_id) or Job(job_id, "unknown", status, _now())

    def _write(self, job: Job) -> None:
        self._r.set(self._prefix + job.job_id, json.dumps(job.to_dict()), ex=self._ttl_s)
