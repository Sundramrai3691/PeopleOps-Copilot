# Contributing

PeopleOps Copilot is an employee operations assistant. Keep analytics read-only.
Sensitive employee writes must pass service authorization, validation, manager
approval, a transaction, and an audit event.

- Keep offline evaluation answers out of planner prompts and general-query validation.
  Fixed catalog tasks may use application-owned reference SQL declared in their
  contract. Report those checks separately from blind model evaluation.
- Distinguish scripted demonstrations from real-model experiments.
- Preserve source provenance, evaluation conditions and negative results for
  current public claims. Superseded evidence may move to Git history when the
  README and evaluation index no longer cite it.
- Test failure behavior, role checks, stale claims and contract drift when changed.
- Never commit database credentials, API keys or private source databases.
- Before publishing, inspect the staged file list and local documentation links.
  Use portable commands and label excluded local artifacts explicitly.
- Synthetic employee and policy fixtures must remain fictional.
- Run `python -m pytest -q` and `python -m compileall -q src` before proposing changes.
