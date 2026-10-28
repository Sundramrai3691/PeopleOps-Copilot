# Harness Security Labs Application Essay

**Prompt:** Tell us about the best thing you've broken, built or figured out.  
**Target:** Harness Security Labs Security Research Internship (October 31, 2026)  
**Author:** Research Applicant  
**Artifact:** ToolFence & PeopleOps Copilot Security Architecture  

---

While auditing PeopleOps Copilot, an employee assistant with database analytics and approval workflows, I found a separation-of-duties flaw in leave processing: `review_leave_request` verified that the approver managed the subject employee, but never checked who initiated the request. A manager could initiate a leave proposal for a subordinate and approve that same proposal without independent review. Furthermore, early role checks raised exceptions before database initialization, silently dropping audit records for unauthorized queries.

I reproduced both failures with synthetic fixtures, then patched them by requiring an independent approver (`proposal["actor"] != approver`) and routing denials through `self._deny()`, which persists structured audit records with SHA-256 payload digests for tamper-detection.

To evaluate these trust boundaries systematically, I built **ToolFence**, a 24-case deterministic benchmark. Rather than checking LLM refusal text, ToolFence inspects application and database state: confirming proposals stay pending, balances remain untouched, and table allowlists block unauthorized queries. Across 17 executed adversarial cases, it verified a 100% block rate (0/7 data leaks, 0/4 approval bypasses) while passing 7/7 benign controls without false positives.

My main takeaway: prompt instructions are heuristics. Autonomous agents require strict, deterministic authorization enforced at the tool and database layers.
