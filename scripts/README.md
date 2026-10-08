# PeopleOps Copilot repository commands

These commands support the PeopleOps Copilot service in `src/sql_agent`. Run Python
commands from the repository root with module syntax, for example:

```bash
python -m scripts.demo.local_demo start
python -m scripts.evaluation.evaluate_bird_agent --help
```

| Directory | Purpose |
| --- | --- |
| `demo/` | Start the local product demo and run disposable smoke checks. |
| `evaluation/` | Run PeopleOps evaluation checks and retained upstream SQL research tools. |
| `training/` | Prepare and score the optional local QLoRA experiment. |
| `validation/` | Validate deployment boundaries, registered datasets, and published evidence. |
| `maintenance/` | Regenerate documentation assets and verify public metrics. |

The scripts are not imported by the production service. Reusable runtime logic
belongs in `src/sql_agent`; scripts should remain thin orchestration entry points.
