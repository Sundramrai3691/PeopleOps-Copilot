from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


TERMINAL_STATUSES = {"completed", "needs_review", "review_approved", "review_rejected", "failed"}


def validate_finish(status, result):
    if status not in {'completed', 'needs_review', 'waiting_user'}:
        raise ValueError('unsupported finish status')
    if status == 'waiting_user':
        question = result.get('clarification', {})
        if not isinstance(question, dict) or any(
                not isinstance(question.get(k), str) or not question[k].strip() or len(question[k]) > limit
                for k, limit in [('id', 128), ('question', 2000)]):
            raise ValueError('waiting_user requires an identified clarification question')


def clarification_reply(waiting_id, answer, actor, key):
    for value, maximum in [(waiting_id,128), (answer,4000), (actor,256), (key,256)]:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError('invalid clarification reply')
    return {'id': waiting_id, 'answer': answer, 'actor': actor}


def resume_payload(job, reply):
    if job.status != 'waiting_user' or (job.result or {}).get('clarification', {}).get('id') != reply['id']:
        raise ValueError('job is not awaiting this clarification')
    if job.attempts >= job.max_attempts:
        raise ValueError('job attempt budget exhausted; cannot resume')
    history = job.payload.get('_clarifications', [])
    if len(history) >= 2:
        raise ValueError('clarification budget exhausted')
    return {**job.payload, '_clarifications': [*history, reply]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Job:
    job_id: str
    job_type: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    attempts: int
    max_attempts: int
    idempotency_key: str
    created_at: str
    updated_at: str
    worker_id: str | None = None
    lease_expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SqliteJobRepository:
    """Durable local job repository with atomic worker claims.

    SQLite is the reference deployment adapter. The API depends only on this
    boundary so a PostgreSQL adapter can replace it without changing workflows.
    """

    # Keep short queue transactions serialized within this process. The local
    # SQLite build can hang when connections are opened and closed concurrently.
    # Worker inference never runs while this lock is held; processes still use WAL.
    _connection_lock = threading.RLock()

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._connection_lock:
            connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=10000")
                yield connection
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "worker_id" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN worker_id TEXT")
            if "lease_expires_at" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN lease_expires_at TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS job_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            connection.execute('''CREATE TABLE IF NOT EXISTS job_resumes (
                job_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, reply TEXT NOT NULL,
                PRIMARY KEY(job_id, idempotency_key))''')

    @staticmethod
    def _event(connection: sqlite3.Connection, job_id: str,
               event_type: str, details: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO job_events(job_id,event_type,details,created_at) VALUES (?,?,?,?)",
            (job_id, event_type, json.dumps(details), _now()),
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"], job_type=row["job_type"], status=row["status"],
            payload=json.loads(row["payload"]),
            result=json.loads(row["result"]) if row["result"] else None,
            error=json.loads(row["error"]) if row["error"] else None,
            attempts=row["attempts"], max_attempts=row["max_attempts"],
            idempotency_key=row["idempotency_key"], created_at=row["created_at"],
            updated_at=row["updated_at"],
            worker_id=row["worker_id"], lease_expires_at=row["lease_expires_at"],
        )

    def submit(self, job_type: str, payload: dict[str, Any],
               idempotency_key: str, max_attempts: int = 3) -> tuple[Job, bool]:
        if not job_type or not idempotency_key:
            raise ValueError("job_type and idempotency_key are required")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        timestamp, job_id = _now(), str(uuid.uuid4())
        with self._connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO jobs
                    (job_id,job_type,status,payload,result,error,attempts,max_attempts,idempotency_key,created_at,updated_at,worker_id,lease_expires_at)
                    VALUES (?, ?, 'queued', ?, NULL, NULL, 0, ?, ?, ?, ?, NULL, NULL)""",
                    (job_id, job_type, json.dumps(payload), max_attempts,
                     idempotency_key, timestamp, timestamp),
                )
                self._event(connection, job_id, "submitted", {
                    "job_type": job_type,
                    "trace_id": payload.get("_trace_id"),
                    "submitted_by": payload.get("_submitted_by"),
                })
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._job(row), created

    def get(self, job_id: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def find_by_trace_id(self, trace_id: str) -> list[Job]:
        """Return the original run and any replays linked to a trace."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM jobs
                WHERE json_extract(payload, '$._trace_id') = ?
                   OR json_extract(payload, '$._replay_of_trace_id') = ?
                ORDER BY created_at""",
                (trace_id, trace_id),
            ).fetchall()
        return [self._job(row) for row in rows]

    def status_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        return {row["status"]: row["count"] for row in rows}

    def events(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_type,details,created_at FROM job_events WHERE job_id=? ORDER BY event_id",
                (job_id,),
            ).fetchall()
        return [{"event_type": row["event_type"], "details": json.loads(row["details"]), "created_at": row["created_at"]} for row in rows]

    def claim_next(self, worker_id: str = "worker", lease_seconds: int = 60) -> Job | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            expired = connection.execute(
                "SELECT job_id,worker_id,attempts,max_attempts FROM jobs WHERE status='running' AND lease_expires_at < ?", (now,)
            ).fetchall()
            for stale in expired:
                status = "failed" if stale["attempts"] >= stale["max_attempts"] else "queued"
                connection.execute(
                    "UPDATE jobs SET status=?,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?",
                    (status, now, stale["job_id"]),
                )
                self._event(connection, stale["job_id"], "lease_expired", {"worker_id": stale["worker_id"], "next_status": status})
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' AND attempts < max_attempts ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            lease_expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, worker_id=?, lease_expires_at=?, updated_at=? WHERE job_id=? AND status='queued'",
                (worker_id, lease_expires, now, row["job_id"]),
            )
            self._event(connection, row["job_id"], "claimed", {"worker_id": worker_id, "lease_seconds": lease_seconds})
            connection.execute("COMMIT")
        return self.get(row["job_id"])

    def renew_lease(self, claim: Job, lease_seconds: int) -> None:
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError('lease must be positive seconds')
        now = _now()
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE job_id=? AND status='running' AND worker_id=? AND attempts=? AND lease_expires_at>?",
                (expiry, now, claim.job_id, claim.worker_id, claim.attempts, now))
            if cursor.rowcount != 1:
                raise RuntimeError('cannot renew stale claim')

    def finish(self, job_id: str, status: str, result: dict[str, Any], *, claim: Job) -> Job:
        if claim.job_id != job_id:
            raise ValueError("claim does not belong to job")
        validate_finish(status, result)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.execute(
                "UPDATE jobs SET status=?, result=?, error=NULL, worker_id=NULL, lease_expires_at=NULL, updated_at=? WHERE job_id=? AND status='running' AND worker_id=? AND attempts=? AND lease_expires_at>?",
                (status, json.dumps(result), _now(), job_id, claim.worker_id, claim.attempts, _now()),
            )
            if cursor.rowcount == 1:
                details = {'attempt_telemetry': result['attempt_telemetry']} if 'attempt_telemetry' in result else {}
                self._event(connection, job_id, status, details)
            connection.execute('COMMIT')
        if cursor.rowcount != 1:
            raise RuntimeError("job is not running or claim is stale")
        return self.get(job_id)

    def resume(self, job_id, waiting_id, answer, actor, idempotency_key):
        reply = clarification_reply(waiting_id, answer, actor, idempotency_key)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('unknown job')
            previous = connection.execute('SELECT reply FROM job_resumes WHERE job_id=? AND idempotency_key=?',
                                          (job_id, idempotency_key)).fetchone()
            if previous:
                if json.loads(previous['reply']) != reply:
                    raise ValueError('resume idempotency key already used for another reply')
            else:
                payload = resume_payload(self._job(row), reply)
                connection.execute('UPDATE jobs SET status=\'queued\',payload=?,result=NULL,error=NULL,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?',
                                   (json.dumps(payload), _now(), job_id))
                connection.execute('INSERT INTO job_resumes VALUES (?,?,?)', (job_id, idempotency_key, json.dumps(reply)))
                self._event(connection, job_id, 'user_resumed', {'clarification_id': waiting_id, 'actor': actor})
            connection.execute('COMMIT')
        return self.get(job_id)

    def record_failure(self, job_id: str, code: str, message: str,
                       retryable: bool = True, *, claim: Job, evidence: dict | None = None) -> Job:
        if claim.job_id != job_id:
            raise ValueError("claim does not belong to job")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=? AND status='running' AND worker_id=? AND attempts=? AND lease_expires_at>?",
                (job_id, claim.worker_id, claim.attempts, _now()),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise RuntimeError("job is not running or claim is stale")
            status = "queued" if retryable and row["attempts"] < row["max_attempts"] else "failed"
            error = {"code": code, "message": message, "retryable": retryable}
            if evidence is not None:
                error['evidence'] = evidence
            connection.execute(
                "UPDATE jobs SET status=?,error=?,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE job_id=?",
                (status, json.dumps(error), _now(), job_id),
            )
            self._event(connection, job_id, "retry_scheduled" if status == "queued" else "failed", error)
            connection.execute("COMMIT")
        return self.get(job_id)

    def review(self, job_id: str, reviewer: str, decision: str,
               notes: str = "") -> Job:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=? AND status='needs_review'", (job_id,)
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise RuntimeError("job is not awaiting review")
            result = json.loads(row["result"])
            review = {"reviewer": reviewer, "decision": decision, "notes": notes,
                      "reviewed_at": _now()}
            result["review"] = review
            status = "review_approved" if decision == "approve" else "review_rejected"
            connection.execute(
                "UPDATE jobs SET status=?,result=?,updated_at=? WHERE job_id=?",
                (status, json.dumps(result), _now(), job_id),
            )
            self._event(connection, job_id, status, review)
            connection.execute("COMMIT")
        return self.get(job_id)
