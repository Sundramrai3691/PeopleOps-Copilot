# Harness Security Labs Application Essay

**Prompt:** Tell us about the best thing you've broken, built or figured out.  
**Target:** Harness Security Labs Security Research Internship (October 31, 2026)  
**Author:** Research Applicant  
**Artifact:** ToolFence & PeopleOps Copilot Security Architecture  

---

While auditing PeopleOps Copilot, an enterprise assistant with database analytics and approval gates, I discovered an identity-conflation flaw in its leave workflow: `review_leave_request` verified that the approver managed the subject employee, but never checked if the approver was the initiating principal. A manager could propose an unentitled leave grant for a subordinate and self-approve it, bypassing separation of duties. Furthermore, early role rejections threw exceptions before database execution, silently dropping audit records for unauthorized probes.

I resolved this by enforcing an independent-approver invariant (`proposal["actor"] != approver`) and routing rejections through an audit sink with SHA-256 payload digests, ensuring dual custody and non-repudiation.

To evaluate these trust boundaries reproducibly, I built **ToolFence**, a 24-case deterministic benchmark. Rather than relying on LLM refusal text, ToolFence inspects actual database state—confirming proposals remain pending, balances are untouched, and table allowlists block unauthorized reads. Across 17 adversarial cases, ToolFence verified a 100% block rate (0/7 data leaks, 0/4 approval bypasses) while executing 7/7 benign controls without false positives.

The key engineering lesson: an LLM's context window cannot serve as an access control boundary. Model instructions are heuristics; authorization, separation of duties, and data isolation must be enforced deterministically at the execution sink.
