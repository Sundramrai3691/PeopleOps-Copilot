# Workflow reliability

PeopleOps Copilot retains the underlying durable SQL workflow's request repository, worker leases, bounded retries, LangGraph checkpointing, idempotency, SQL policy checks, and explicit review path.

## Request processing

The API validates request shape and principal role before storing work. The worker claims a bounded lease, runs a LangGraph workflow, and records the outcome with telemetry. Lease renewal failure discards stale worker results. Retryable pipeline failures remain subject to the stored attempt limit.

## SQL guardrails

Registered database/task policies constrain the allowed source, tables, and operation. Read queries are parsed as a single read-only statement before execution. Result size and model repair attempts are bounded. A generated query is not automatically treated as semantically correct merely because it executes.

## PeopleOps writes

The leave-request service validates identity, date range, leave type, leave balance, and manager assignment before persisting a pending action proposal. Manager review is authorization checked. Approval performs the leave request insert, balance decrement, status transition, and audit record inside one database transaction. The PeopleOps approval is atomic but currently does not use LangGraph checkpoint resume.

## Operational limits

Browser sessions and local SQLite are single-process conveniences. PostgreSQL is recommended for shared runs. Never place real employee information in the bundled synthetic fixture or publish access keys in logs.
