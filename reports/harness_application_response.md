# Harness Security Labs Security Research Application
## Question: Tell us about the best thing you've broken, built or figured out.

**Candidate Name:** Research Applicant  
**Submission Target:** Harness Security Labs Security Research Internship (Summer / Fall 2026)  
**Project Artifact:** ToolFence & PeopleOps Copilot Security Architecture  
**Repository:** `SQL-Agent` (PeopleOps Copilot Security Hardening & Evaluation Suite)  

---

### Executive Overview

In securing autonomous AI systems, the most dangerous assumption is treating the language model as a trusted security boundary. During my security audit and hardening of **PeopleOps Copilot**—an enterprise AI assistant orchestrating natural language queries, Postgres/SQLite analytics, model context protocol (MCP) tools, and human-in-the-loop approvals—I broke an architectural separation-of-duties flaw that permitted unilateral privilege escalation, built **ToolFence** (a deterministic 24-case agent trust-boundary evaluation benchmark), and figured out the mechanics of establishing provable execution fences around non-deterministic LLM agents.

---

### 1. What I Broke: Conflated Identity and Silent Observability Failures

While auditing the multi-step approval workflow in `src/sql_agent/peopleops.py`, I identified an identity-conflation vulnerability that completely bypassed separation of duties (SoD) in sensitive operations:

#### The Vulnerability
The leave request lifecycle tracked two distinct identities: the `employee_id` (the subject of the request) and the `actor` (the principal who initiated the mutation proposal). However, the authorization rule in `review_leave_request` evaluated only:
```python
# Vulnerable baseline logic:
if employee["manager_id"] != approver and approver_role != "hr":
    raise PermissionError("Only direct manager or HR can review leave requests")
```
Because the check validated whether the approver was the direct manager of the *subject* (`employee_id`), a manager (`E001`) could initiate an unentitled leave grant on behalf of their direct subordinate (`E002`), and then immediately issue an approval transaction under their own identity. The system treated the transaction as legitimate because `E001` was indeed `E002`'s manager. The initiator and approver were the exact same physical principal. This broke the foundational principle of two-man control for sensitive corporate mutations.

#### The Observability Blindspot
Simultaneously, I discovered a critical audit-logging failure: when an unprivileged employee attempted an unauthorized mutation or requested team headcount analytics, the service threw `PermissionError` *before* initiating a database transaction. Because audit persistence was tied to post-authorization execution hooks, zero audit events were recorded in `audit_events`. An attacker could probe organizational boundaries and brute-force access control thresholds without generating a single forensic trace in the compliance log.

---

### 2. What I Built: The ToolFence Evaluation Benchmark

Rather than applying superficial prompt patches (e.g., system-prompt admonitions telling the model "do not allow self-approval"), I implemented server-side defensive fences and built **ToolFence**, a 24-case deterministic benchmark evaluating agent trust boundaries at the data and tool layer.

#### Defense-in-Depth Hardening
1. **Separation-of-Duties Guard:** Enforced strict dual-custody invariants in `PeopleOpsService.review_leave_request`:
   ```python
   if decision == "approve" and proposal.get("actor") == approver:
       self._deny(
           actor=approver,
           action="leave_approval_review",
           resource_id=f"proposal:{proposal_id}",
           reason="Separation of duties violation: proposal initiator cannot self-approve",
       )
   ```
2. **Fail-Closed Audit Pipeline:** Refactored early permission checks to route through `self._deny()`, recording an immutable audit entry with actor ID, timestamp, and a SHA-256 payload digest before returning HTTP 403.
3. **MCP Least-Privilege Isolation:** Re-architected stdio MCP tool dispatch to mandate caller-specific API tokens, preventing tool callers from accessing peer records or cross-department aggregates.

#### Deterministic Benchmark Architecture
ToolFence evaluates 24 empirical scenarios divided across 5 core threat vectors:
- **Category A (Direct Instruction Overrides):** Jailbreak prompts attempting system prompt overrides (`TF-01`), SQL injection strings (`TF-02`), and unentitled PII exfiltration (`TF-03`, `TF-04`).
- **Category B (Indirect Prompt Injection):** Malicious payloads hidden inside retrieved policy documentation attempting SQL drop-table execution (`TF-06`), payload extraction (`TF-07`), and prompt manipulation (`TF-08`, `TF-09`).
- **Category C (Tool Authorization & Trust Boundaries):** Model-generated tool parameters attempting parameter injection (`TF-10`), MCP key spoofing (`TF-11`, `TF-12`), SQLite authorizer bypasses (`TF-13`), and self-approval (`TF-14`).
- **Category D (Multi-Turn State Isolation):** Cross-turn adversarial conditioning testing whether failed authorization or adversarial context in Turn 1 contaminates Turn 2 (`TF-16` through `TF-19`).
- **Category E (Benign Controls):** Legitimate employee self-service queries and manager reviews ensuring zero false positives (`TF-20` through `TF-24`).

Crucially, ToolFence does not evaluate security by inspecting whether an LLM replied "I cannot fulfill this request." Instead, it inspects real database state: asserting that SQLite balance columns remain untouched, proposal records stay in `pending` status, and forensic audit entries are persisted.

---

### 3. What I Figured Out: The Illusion of Model-Level Trust Boundaries

Building ToolFence led to a core insight: **An AI model's context window cannot serve as an access control boundary.**

1. **Prompt Guards are Heuristics, Not Controls:**
   In prompt injection research, defenders often attempt to filter, sanitize, or instruct the LLM against malicious execution. However, an LLM context is fundamentally a single untyped text stream where data and instructions mix. Relying on an LLM to enforce role-based access control is analogous to asking an untrusted client application to enforce database permissions.
2. **Enforce Fences at the Execution Sink:**
   Security invariants must exist outside the agent's cognition:
   - **SQL Layer:** Parameterization and SQLite authorizer hooks (`set_authorizer`) that disallow unauthorized table reads (`PRAGMA`, schema tables) regardless of what SQL the LLM generates.
   - **Service Layer:** Cryptographic identity verification and role validation at the FastAPI endpoint and MCP boundary before tool execution.
   - **State Invariants:** Database-level checks that prevent self-approval even if a compromised agent emits an `approve` tool call.
3. **Audit Visibility Must Precede Denial:**
   If a security rejection terminates an execution path before audit ingestion, the system is blind to active reconnaissance. Every rejection must be treated as a high-fidelity telemetry event.

---

### Benchmark Empirical Validation

Across the 24 deterministic test cases, ToolFence demonstrated:
- **100% Block Rate (17/17)** on adversarial injection, approval-bypass, and privilege escalation attempts.
- **100% Completion Rate (7/7)** on authorized benign controls (0% false positive rate).
- **0 Data Leaks** and **0 Mutation Bypasses** across all test runs.
- **445 Passed Repository Tests** verifying zero regressions across legacy SQL routing, agent workflows, and API handlers.

By treating the AI agent as an untrusted client and anchoring security in deterministic, server-side fences, we transform non-deterministic assistants into enterprise-grade systems capable of safely handling sensitive corporate data.

