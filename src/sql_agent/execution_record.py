"""Shared contract/evidence record for domain-specific execution controllers.

Records are persisted as part of Job.result by the existing worker. They are
audit records, not resumable checkpoints or a replacement job queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _audit_value(value: Any) -> Any:
    # PostgreSQL JSONB rejects NaN/Infinity. Preserve bad-input evidence as a
    # tagged value instead of breaking persistence on a validation failure.
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite_number": repr(value)}
    if isinstance(value, dict):
        return {key: _audit_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_audit_value(item) for item in value]
    return value


@dataclass(frozen=True)
class ContractSnapshot:
    domain: str
    specification_json: str

    @classmethod
    def capture(cls, domain: str, specification: dict[str, Any]) -> ContractSnapshot:
        return cls(domain, json.dumps(specification, sort_keys=True, allow_nan=False))

    @property
    def sha256(self) -> str:
        return digest({"domain": self.domain, "specification": json.loads(self.specification_json)})

    def to_dict(self) -> dict[str, Any]:
        return {"domain": self.domain, "specification": json.loads(self.specification_json),
                "sha256": self.sha256}


@dataclass
class ExecutionRecord:
    contract: ContractSnapshot
    job_attempt: int = 0
    job_attempt_limit: int = 0
    repair_limit: int = 0
    phase: str = "collect"
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, phase: str, details: dict[str, Any]) -> None:
        if self.phase == "finished":
            raise RuntimeError("execution record is finalized")
        if phase not in {"collect", "act", "verify", "decide"}:
            raise ValueError("unknown execution phase")
        # Snapshot values; later controller mutations cannot rewrite prior evidence.
        snapshot = json.loads(json.dumps(_audit_value(details), sort_keys=True,
                                         default=str, allow_nan=False))
        self.events.append({"sequence": len(self.events) + 1, "phase": phase,
                            "details": snapshot, "sha256": digest(snapshot)})
        self.phase = phase

    def finish(self, decision: str, reason: str, repairs: int) -> dict[str, Any]:
        if decision not in {"KEEP", "STOP"}:
            raise ValueError("domain controller must finish with KEEP or STOP")
        if not 0 <= repairs <= self.repair_limit:
            raise ValueError("repair count exceeds execution budget")
        self.record("decide", {"decision": decision, "reason": reason})
        self.phase = "finished"
        return {"schema_version": 1, "contract": self.contract.to_dict(),
                "phase": self.phase, "decision": decision, "reason": reason,
                "budgets": {"job_attempt": self.job_attempt,
                            "job_attempt_limit": self.job_attempt_limit,
                            "agent_repairs": repairs, "agent_repair_limit": self.repair_limit},
                "events": self.events}
