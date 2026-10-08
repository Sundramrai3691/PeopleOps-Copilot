from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .jobs import Job, clarification_reply, resume_payload, validate_finish


class PostgresJobRepository:
    """Concurrent job repository using row locks and SKIP LOCKED."""

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install the production extra: pip install -e '.[prod]'") from exc
        self.database_url, self._psycopg, self._row_factory = database_url, psycopg, dict_row
        self._initialize()

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._row_factory)

    def _initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id UUID PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    result JSONB,
                    error JSONB,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    worker_id TEXT,
                    lease_expires_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS job_resumes (
                    job_id UUID NOT NULL REFERENCES jobs(job_id),
                    idempotency_key TEXT NOT NULL,
                    reply JSONB NOT NULL,
                    PRIMARY KEY(job_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    event_id BIGSERIAL PRIMARY KEY,
                    job_id UUID NOT NULL REFERENCES jobs(job_id),
                    event_type TEXT NOT NULL,
                    details JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                );
            """)

    @staticmethod
    def _job(row: dict[str, Any]) -> Job:
        return Job(
            job_id=str(row["job_id"]), job_type=row["job_type"], status=row["status"],
            payload=row["payload"], result=row["result"], error=row["error"],
            attempts=row["attempts"], max_attempts=row["max_attempts"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"].isoformat(), updated_at=row["updated_at"].isoformat(),
            worker_id=row["worker_id"],
            lease_expires_at=row["lease_expires_at"].isoformat() if row["lease_expires_at"] else None,
        )

    @staticmethod
    def _event(cursor, job_id: str, event_type: str, details: dict) -> None:
        from psycopg.types.json import Jsonb
        cursor.execute(
            "INSERT INTO job_events(job_id,event_type,details,created_at) VALUES (%s,%s,%s,%s)",
            (job_id, event_type, Jsonb(details), datetime.now(timezone.utc)),
        )

    def submit(self, job_type: str, payload: dict, idempotency_key: str,
               max_attempts: int = 3) -> tuple[Job, bool]:
        from psycopg.types.json import Jsonb
        if not job_type or not idempotency_key:
            raise ValueError("job_type and idempotency_key are required")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        job_id, now = str(uuid.uuid4()), datetime.now(timezone.utc)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO jobs(job_id,job_type,status,payload,attempts,max_attempts,idempotency_key,created_at,updated_at)
                VALUES (%s,%s,'queued',%s,0,%s,%s,%s,%s)
                ON CONFLICT(idempotency_key) DO NOTHING RETURNING *
            """, (job_id, job_type, Jsonb(payload), max_attempts, idempotency_key, now, now))
            row = cursor.fetchone()
            created = row is not None
            if created:
                self._event(cursor, job_id, "submitted", {
                    "job_type": job_type,
                    "trace_id": payload.get("_trace_id"),
                    "submitted_by": payload.get("_submitted_by"),
                })
            else:
                cursor.execute("SELECT * FROM jobs WHERE idempotency_key=%s", (idempotency_key,))
                row = cursor.fetchone()
        return self._job(row), created

    def get(self, job_id: str) -> Job | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM jobs WHERE job_id=%s", (job_id,))
            row = cursor.fetchone()
        return self._job(row) if row else None

    def find_by_trace_id(self, trace_id: str) -> list[Job]:
        """Return the original run and any replays linked to a trace."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT * FROM jobs
                WHERE payload->>'_trace_id'=%s OR payload->>'_replay_of_trace_id'=%s
                ORDER BY created_at""",
                (trace_id, trace_id),
            )
            rows = cursor.fetchall()
        return [self._job(row) for row in rows]

    def claim_next(self, worker_id: str = "worker", lease_seconds: int = 60) -> Job | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = datetime.now(timezone.utc)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                WITH expired AS (
                    SELECT job_id,worker_id FROM jobs
                    WHERE status='running' AND lease_expires_at < %s FOR UPDATE
                )
                UPDATE jobs SET status=CASE WHEN jobs.attempts >= jobs.max_attempts THEN 'failed' ELSE 'queued' END,worker_id=NULL,lease_expires_at=NULL,updated_at=%s
                FROM expired WHERE jobs.job_id=expired.job_id
                RETURNING jobs.job_id,jobs.status,expired.worker_id AS expired_worker_id
            """, (now, now))
            for stale in cursor.fetchall():
                self._event(cursor, str(stale["job_id"]), "lease_expired", {"worker_id": stale["expired_worker_id"], "next_status": stale["status"]})
            cursor.execute("""
                SELECT job_id FROM jobs WHERE status='queued' AND attempts < max_attempts
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
            """)
            selected = cursor.fetchone()
            if selected is None:
                return None
            cursor.execute("""
                UPDATE jobs SET status='running',attempts=attempts+1,worker_id=%s,
                    lease_expires_at=%s,updated_at=%s WHERE job_id=%s RETURNING *
            """, (worker_id, now + timedelta(seconds=lease_seconds), now, selected["job_id"]))
            row = cursor.fetchone()
            self._event(cursor, str(row["job_id"]), "claimed", {"worker_id": worker_id, "lease_seconds": lease_seconds})
        return self._job(row)

    def renew_lease(self, claim: Job, lease_seconds: int) -> None:
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError('lease must be positive seconds')
        now = datetime.now(timezone.utc)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE jobs SET lease_expires_at=%s,updated_at=%s
                WHERE job_id=%s AND status='running' AND worker_id=%s AND attempts=%s AND lease_expires_at>%s""",
                (now + timedelta(seconds=lease_seconds), now, claim.job_id, claim.worker_id, claim.attempts, now))
            if cursor.rowcount != 1:
                raise RuntimeError('cannot renew stale claim')

    def finish(self, job_id: str, status: str, result: dict, *, claim: Job) -> Job:
        from psycopg.types.json import Jsonb
        if claim.job_id != job_id:
            raise ValueError("claim does not belong to job")
        validate_finish(status, result)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                UPDATE jobs SET status=%s,result=%s,error=NULL,worker_id=NULL,
                    lease_expires_at=NULL,updated_at=%s
                WHERE job_id=%s AND status='running' AND worker_id=%s AND attempts=%s AND lease_expires_at>%s RETURNING *
            """, (status, Jsonb(result), datetime.now(timezone.utc), job_id, claim.worker_id, claim.attempts, datetime.now(timezone.utc)))
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("job is not running")
            details = {'attempt_telemetry': result['attempt_telemetry']} if 'attempt_telemetry' in result else {}
            self._event(cursor, job_id, status, details)
        return self._job(row)

    def resume(self, job_id: str, waiting_id: str, answer: str,
               actor: str, idempotency_key: str) -> Job:
        from psycopg.types.json import Jsonb
        reply = clarification_reply(waiting_id, answer, actor, idempotency_key)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM jobs WHERE job_id=%s FOR UPDATE", (job_id,))
            row = cursor.fetchone()
            if row is None:
                raise ValueError('job not found')
            cursor.execute("SELECT reply FROM job_resumes WHERE job_id=%s AND idempotency_key=%s",
                           (job_id, idempotency_key))
            previous = cursor.fetchone()
            if previous is not None:
                if previous['reply'] != reply:
                    raise ValueError('idempotency key already used for a different reply')
            else:
                payload = resume_payload(self._job(row), reply)
                cursor.execute("""UPDATE jobs SET status='queued',payload=%s,result=NULL,error=NULL,
                    worker_id=NULL,lease_expires_at=NULL,updated_at=%s WHERE job_id=%s RETURNING *""",
                    (Jsonb(payload), datetime.now(timezone.utc), job_id))
                row = cursor.fetchone()
                cursor.execute("INSERT INTO job_resumes(job_id,idempotency_key,reply) VALUES (%s,%s,%s)",
                               (job_id, idempotency_key, Jsonb(reply)))
                self._event(cursor, job_id, 'user_resumed', {'clarification_id': waiting_id, 'actor': actor})
        return self._job(row)

    def record_failure(self, job_id: str, code: str, message: str,
                       retryable: bool = True, *, claim: Job, evidence: dict | None = None) -> Job:
        if claim.job_id != job_id:
            raise ValueError("claim does not belong to job")
        from psycopg.types.json import Jsonb
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM jobs WHERE job_id=%s AND status='running' AND worker_id=%s AND attempts=%s AND lease_expires_at>%s FOR UPDATE", (job_id, claim.worker_id, claim.attempts, datetime.now(timezone.utc)))
            current = cursor.fetchone()
            if current is None:
                raise RuntimeError("job is not running")
            status = "queued" if retryable and current["attempts"] < current["max_attempts"] else "failed"
            error = {"code": code, "message": message, "retryable": retryable}
            if evidence is not None:
                error['evidence'] = evidence
            cursor.execute("""
                UPDATE jobs SET status=%s,error=%s,worker_id=NULL,lease_expires_at=NULL,
                    updated_at=%s WHERE job_id=%s RETURNING *
            """, (status, Jsonb(error), datetime.now(timezone.utc), job_id))
            row = cursor.fetchone()
            self._event(cursor, job_id, "retry_scheduled" if status == "queued" else "failed", error)
        return self._job(row)

    def status_counts(self) -> dict[str, int]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")
            return {row["status"]: row["count"] for row in cursor.fetchall()}

    def events(self, job_id: str) -> list[dict]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT event_type,details,created_at FROM job_events WHERE job_id=%s ORDER BY event_id", (job_id,))
            rows = cursor.fetchall()
        return [{"event_type": row["event_type"], "details": row["details"], "created_at": row["created_at"].isoformat()} for row in rows]

    def review(self, job_id: str, reviewer: str, decision: str,
               notes: str = "") -> Job:
        from psycopg.types.json import Jsonb
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM jobs WHERE job_id=%s AND status='needs_review' FOR UPDATE", (job_id,))
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("job is not awaiting review")
            result = dict(row["result"])
            reviewed_at = datetime.now(timezone.utc)
            review = {"reviewer": reviewer, "decision": decision, "notes": notes,
                      "reviewed_at": reviewed_at.isoformat()}
            result["review"] = review
            status = "review_approved" if decision == "approve" else "review_rejected"
            cursor.execute(
                "UPDATE jobs SET status=%s,result=%s,updated_at=%s WHERE job_id=%s RETURNING *",
                (status, Jsonb(result), reviewed_at, job_id),
            )
            updated = cursor.fetchone()
            self._event(cursor, job_id, status, review)
        return self._job(updated)
