from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ActionProposal:
    action: str
    tool: str = ""
    arguments: dict[str, Any] | None = None
    source: str = "planner"


@dataclass(frozen=True)
class RuntimeOutcome:
    decision: str
    reason: str
    output: Any
    trajectory: tuple[dict[str, Any], ...]


class ExecutableEnvironment(Protocol):
    def authorize(self, proposal: ActionProposal) -> tuple[bool, str]: ...
    def execute(self, proposal: ActionProposal) -> Any: ...
    def verify(self, proposal: ActionProposal, output: Any) -> tuple[bool, str]: ...


class BoundedAgentRuntime:
    """Domain-independent proposal → policy → execute → verify state machine."""

    def run(self, proposal: ActionProposal, environment: ExecutableEnvironment) -> RuntimeOutcome:
        trace: list[dict[str, Any]] = [{"step": "propose", "action": proposal.action,
                                       "tool": proposal.tool, "source": proposal.source}]
        if proposal.action.upper() == "STOP":
            trace.append({"step": "decision", "action": "STOP", "reason": "planner_stop"})
            return RuntimeOutcome("STOP", "planner_stop", None, tuple(trace))

        allowed, reason = environment.authorize(proposal)
        trace.append({"step": "authorize", "allowed": allowed, "reason": reason})
        if not allowed:
            trace.append({"step": "decision", "action": "STOP", "reason": reason})
            return RuntimeOutcome("STOP", reason, None, tuple(trace))

        try:
            output = environment.execute(proposal)
            trace.append({"step": "execute", "tool": proposal.tool, "status": "ok"})
        except Exception as exc:
            reason = f"tool_error:{type(exc).__name__}"
            trace.append({"step": "execute", "tool": proposal.tool, "status": "error",
                          "error_type": type(exc).__name__})
            trace.append({"step": "decision", "action": "STOP", "reason": reason})
            return RuntimeOutcome("STOP", reason, None, tuple(trace))

        verified, reason = environment.verify(proposal, output)
        trace.append({"step": "verify", "verified": verified, "reason": reason})
        decision = "KEEP" if verified else "STOP"
        trace.append({"step": "decision", "action": decision, "reason": reason})
        return RuntimeOutcome(decision, reason, output, tuple(trace))
