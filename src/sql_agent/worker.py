from __future__ import annotations

import argparse
import logging
import os
import socket
import time
import threading

from .backends import JobRepository, job_repository_from_env
from .logging_config import configure_json_logging
from .pipeline import JobPipeline


class Worker:
    def __init__(self, repository: JobRepository, pipeline: JobPipeline,
                 worker_id: str | None = None, lease_seconds: int = 300) -> None:
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError('lease must be positive seconds')
        self.lease_seconds = lease_seconds
        self.repository, self.pipeline = repository, pipeline
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"

    def _failure(self, job, code, message, retryable, evidence=None):
        try:
            if evidence is not None:
                evidence = self._account_usage(job, evidence)
            self.repository.record_failure(job.job_id, code, message, retryable=retryable,
                                           claim=job, **({'evidence': evidence} if evidence is not None else {}))
        except RuntimeError:
            # A replacement owns the job now; do not overwrite its state.
            logging.getLogger("peopleops_copilot.worker").warning("stale_claim_discarded", extra={"job_id": job.job_id})

    def _account_usage(self, job, result):
        if 'telemetry' not in result:
            return result
        from .telemetry import combine_attempts
        previous = []
        for event in self.repository.events(job.job_id):
            details = event['details']
            usage = details.get('attempt_telemetry') or details.get('evidence', {}).get('attempt_telemetry')
            if usage is not None:
                previous.append(usage)
        current = result['telemetry']
        return {**result, 'attempt_telemetry': current,
                'telemetry': combine_attempts(previous, current, expected_previous=job.attempts-1)}

    def run_once(self) -> bool:
        job = self.repository.claim_next(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        logger = logging.getLogger("peopleops_copilot.worker")
        extra = {"job_id": job.job_id, "trace_id": job.payload.get("_trace_id"), "worker_id": self.worker_id}
        logger.info("job_claimed", extra={**extra, "event_type": "claimed"})
        stopped, lost = threading.Event(), threading.Event()
        def heartbeat():
            while not stopped.wait(self.lease_seconds / 3):
                try:
                    self.repository.renew_lease(job, self.lease_seconds)
                except Exception:
                    lost.set()
                    logger.warning('lease_renewal_failed', extra=extra)
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            outcome = self.pipeline.run(job)
            if lost.is_set():
                raise RuntimeError('lease renewal failed; result discarded')
            self.repository.finish(job.job_id, outcome.status, self._account_usage(job, outcome.result), claim=job)
            logger.info("job_finished", extra={**extra, "event_type": outcome.status})
        except (KeyError, TypeError, ValueError) as exc:
            self._failure(job, "invalid_job", str(exc), False, getattr(exc, 'pipeline_evidence', None))
            logger.warning("job_invalid", extra={**extra, "event_type": "failed"})
        except Exception as exc:  # operational failures are retried within the job budget
            self._failure(job, "pipeline_failure", str(exc), True, getattr(exc, 'pipeline_evidence', None))
            logger.exception("job_pipeline_failure", extra={**extra, "event_type": "retry_scheduled"})
        finally:
            stopped.set()
            thread.join(timeout=1)
        return True

    def run_forever(self, poll_seconds: float = 0.5) -> None:
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)


def main() -> None:
    configure_json_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args()
    worker = Worker(
        job_repository_from_env(),
        JobPipeline(),
    )
    if args.once:
        worker.run_once()
    else:
        worker.run_forever(args.poll_seconds)


if __name__ == "__main__":
    main()
