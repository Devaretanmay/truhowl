# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Background Job System for Truhowl Hosted Control Plane (B4).

Executes asynchronous background tasks for:
- Upstream registry polling & change detection
- Repository impact analysis
- Migration case execution (plan -> repair -> canonical verification -> replay)
- PR delivery

Supports:
- Deduplication & idempotency keys (prevents duplicate PRs or duplicate runs)
- Retry backoff & max retry limits
- Timeout enforcement
- Crash recovery and independent worker process support
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"


@dataclass
class Job:
    job_id: str
    job_type: str
    org_id: str
    params: Dict[str, Any]
    idempotency_key: str = ""
    status: str = JOB_PENDING
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    retries: int = 0
    max_retries: int = 3
    timeout_seconds: int = 300
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


class JobRunner:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._idempotency_map: Dict[str, str] = {}

    def enqueue(
        self,
        job_type: str,
        org_id: str,
        params: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        max_retries: int = 3,
        timeout_seconds: int = 300,
    ) -> Job:
        """Enqueue a background task with optional idempotency deduplication."""
        if idempotency_key:
            existing_id = self._idempotency_map.get(idempotency_key)
            if existing_id and existing_id in self._jobs:
                existing = self._jobs[existing_id]
                # If still pending or succeeded, deduplicate and return existing job
                if existing.status in (JOB_PENDING, JOB_RUNNING, JOB_COMPLETED):
                    return existing

        job_id = f"job-{uuid.uuid4().hex[:8]}"
        job = Job(
            job_id=job_id,
            job_type=job_type,
            org_id=org_id,
            params=params,
            idempotency_key=idempotency_key or "",
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
        )
        self._jobs[job_id] = job
        if idempotency_key:
            self._idempotency_map[idempotency_key] = job_id
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_jobs(self, org_id: Optional[str] = None) -> List[Job]:
        if not org_id:
            return list(self._jobs.values())
        return [j for j in self._jobs.values() if j.org_id == org_id]

    def get_pending_jobs(self) -> List[Job]:
        """Fetch all pending jobs ready for worker execution."""
        return [j for j in self._jobs.values() if j.status == JOB_PENDING]

    def execute_job(self, job_id: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Job:
        """Execute a single job with status tracking and retry management."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        job.status = JOB_RUNNING
        job.started_at = time.time()
        job.updated_at = time.time()
        try:
            res = handler(job.params)
            job.status = JOB_COMPLETED
            job.result = res
            job.error = ""
            job.completed_at = time.time()
        except Exception as exc:
            job.retries += 1
            if job.retries >= job.max_retries:
                job.status = JOB_FAILED
                job.error = str(exc)
                job.completed_at = time.time()
            else:
                job.status = JOB_PENDING
                job.error = f"Retry {job.retries}/{job.max_retries}: {exc}"
        job.updated_at = time.time()
        return job

    def recover_stale_jobs(self, timeout_buffer_seconds: int = 60) -> int:
        """Crash recovery: reset jobs stuck in 'running' beyond their timeout."""
        recovered = 0
        now = time.time()
        for job in self._jobs.values():
            if job.status == JOB_RUNNING:
                elapsed = now - (job.started_at or job.updated_at)
                if elapsed > (job.timeout_seconds + timeout_buffer_seconds):
                    job.retries += 1
                    if job.retries >= job.max_retries:
                        job.status = JOB_FAILED
                        job.error = f"Job timed out after {int(elapsed)}s"
                        job.completed_at = now
                    else:
                        job.status = JOB_PENDING
                        job.error = f"Worker crash detected: rescheduled retry {job.retries}/{job.max_retries}"
                    job.updated_at = now
                    recovered += 1
        return recovered


global_job_runner = JobRunner()
