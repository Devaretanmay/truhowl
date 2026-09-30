# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Automated tests for Background Worker Resilience and Idempotency (B4)."""

import time

from truhowl.api.jobs import JOB_COMPLETED, JOB_FAILED, JOB_PENDING, JobRunner
from truhowl.api.worker import TruhowlWorker


def test_job_idempotency_deduplication():
    runner = JobRunner()

    job1 = runner.enqueue("run_migration", "org_1", {"case": "stripe-1"}, idempotency_key="idemp-case-stripe-1")
    job2 = runner.enqueue("run_migration", "org_1", {"case": "stripe-1"}, idempotency_key="idemp-case-stripe-1")

    # Should deduplicate and return exact same job
    assert job1.job_id == job2.job_id
    assert len(runner.list_jobs("org_1")) == 1


def test_job_retries_and_failure():
    runner = JobRunner()
    job = runner.enqueue("failing_job", "org_1", {}, max_retries=2)

    def failing_handler(_params):
        raise RuntimeError("Transient network issue")

    # Attempt 1: should retry and stay pending
    res1 = runner.execute_job(job.job_id, failing_handler)
    assert res1.status == JOB_PENDING
    assert res1.retries == 1
    assert "Retry 1/2" in res1.error

    # Attempt 2: reaches max retries, transitions to failed
    res2 = runner.execute_job(job.job_id, failing_handler)
    assert res2.status == JOB_FAILED
    assert res2.retries == 2
    assert "Transient network issue" in res2.error


def test_job_crash_recovery():
    runner = JobRunner()
    job = runner.enqueue("long_job", "org_1", {}, timeout_seconds=1)
    job.status = "running"
    job.started_at = time.time() - 100  # Stale running job

    recovered = runner.recover_stale_jobs(timeout_buffer_seconds=5)
    assert recovered == 1
    assert job.status == JOB_PENDING
    assert "Worker crash detected" in job.error


def test_truhowl_worker_execution():
    runner = JobRunner()
    worker = TruhowlWorker(runner=runner)

    executed = []

    def mock_handler(params):
        executed.append(params["msg"])
        return {"status": "ok"}

    worker.register_handler("test_action", mock_handler)
    job = runner.enqueue("test_action", "org_test", {"msg": "hello from worker"})

    processed = worker.process_one()
    assert processed is not None
    assert processed.job_id == job.job_id
    assert processed.status == JOB_COMPLETED
    assert executed == ["hello from worker"]
