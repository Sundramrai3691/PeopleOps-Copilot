# SQL benchmark data

`benchmarks/sql_repair_v1.json`, v2 and v3 contain frozen synthetic SQL tasks.
The generation files preserve historical model proposals. Reference queries and
setup SQL belong to offline evaluation, not the service's planner context.

The default running service creates a separate tiny synthetic database in memory.
Real sources are registered by the operator; see
[the usage guide](../docs/USAGE.md).
