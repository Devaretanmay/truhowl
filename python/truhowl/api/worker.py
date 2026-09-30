# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Dedicated Background Worker Process for Truhowl Control Plane (B4).

Runs decoupled from the web application, handling:
- Asynchronous upstream package registry polling
- Migration case repair & clean-room replay verification
- PR delivery with idempotency guarantees (no duplicate PRs)
- Automatic crash recovery and retry management
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from truhowl.api.jobs import Job, JobRunner, global_job_runner

logger = logging.getLogger("truhowl.worker")


class TruhowlWorker:
    """Autonomous maintenance worker process."""

    def __init__(self, runner: Optional[JobRunner] = None, poll_interval: float = 1.0):
        self.runner = runner or global_job_runner
        self.poll_interval = poll_interval
        self._running = False
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        self._register_default_handlers()

    def register_handler(self, job_type: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self._handlers[job_type] = handler

    def _register_default_handlers(self) -> None:
        def handle_upstream_poll(params: Dict[str, Any]) -> Dict[str, Any]:
            from truhowl.changes.monitor import poll_and_watch

            workspace = params.get("workspace", ".")
            providers = params.get("providers", ["stripe", "openai"])
            repo_paths = params.get("repo_paths", [workspace])
            return poll_and_watch(workspace, providers=providers, repo_paths=repo_paths)

        def handle_migration(params: Dict[str, Any]) -> Dict[str, Any]:
            from truhowl.agent.service import run_repo

            workspace = params.get("workspace", ".")
            case_id = params["case_id"]
            repo_key = params.get("repo_key")
            confirmed = bool(params.get("confirmed", False))
            create_pr = bool(params.get("create_pr", False))
            github_repo = params.get("github_repo")
            return run_repo(
                workspace,
                case_id,
                repo_key,
                confirmed=confirmed,
                create_pr=create_pr,
                github_repo=github_repo,
            )

        self.register_handler("upstream_poll", handle_upstream_poll)
        self.register_handler("run_migration", handle_migration)

    def process_one(self) -> Optional[Job]:
        """Fetch and execute a single pending job, or recover timed-out jobs."""
        self.runner.recover_stale_jobs()
        pending = self.runner.get_pending_jobs()
        if not pending:
            return None
        job = pending[0]
        handler = self._handlers.get(job.job_type)
        if not handler:
            job.status = "failed"
            job.error = f"No handler registered for job type: {job.job_type}"
            return job

        return self.runner.execute_job(job.job_id, handler)

    def run_loop(self, max_iterations: Optional[int] = None) -> None:
        """Run the worker loop until stopped or max_iterations reached."""
        self._running = True
        iterations = 0
        while self._running:
            try:
                processed = self.process_one()
                if not processed:
                    time.sleep(self.poll_interval)
            except Exception as exc:
                logger.error("Unexpected worker loop error: %s", exc)
                time.sleep(self.poll_interval)

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break

    def stop(self) -> None:
        self._running = False


def start_worker() -> None:
    """CLI entrypoint to launch worker daemon."""
    worker = TruhowlWorker()
    worker.run_loop()


if __name__ == "__main__":
    start_worker()
