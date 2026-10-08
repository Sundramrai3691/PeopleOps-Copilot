# BIRD Mini-Dev model and Agent comparison

This report freezes the September 13, 2026 local comparison. Both workflows used
the same official BIRD Mini-Dev 500 questions, SQLite database files and model
weights. Gold SQL was excluded from generation, probing, repair and verification;
it was used after inference to compare execution results.

| Planner model | SQL-Agent correct | SQL-Agent accuracy | PV-SQL correct | PV-SQL accuracy | SQL-Agent − PV-SQL | PV-SQL gains / losses |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Qwen3-4B | 196 / 500 | **39.2%** | 180 / 500 | 36.0% | **+3.2 pp** | 53 / 69 |
| Gemma3-4B | 108 / 500 | 21.6% | 110 / 500 | **22.0%** | −0.4 pp | 47 / 45 |
| Qwen3-0.6B | 37 / 500 | **7.4%** | 34 / 500 | 6.8% | **+0.6 pp** | 17 / 20 |

| Planner model | SQL-Agent tokens / correct | PV-SQL tokens / correct | PV-SQL token multiple | SQL-Agent p95 | PV-SQL p95 | PV-SQL latency multiple |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Qwen3-4B | **6,763** | 19,025 | 2.81× | **3.66 s** | 8.67 s | 2.37× |
| Gemma3-4B | **13,253** | 32,022 | 2.42× | **4.11 s** | 12.80 s | 3.11× |
| Qwen3-0.6B | **33,898** | 44,625 | 1.32× | **5.20 s** | 5.45 s | 1.05× |

The SQL-Agent workflow used full schema context without RAG, bounded repair for eligible
execution errors, and Qwen2.5-Coder-7B as an independent advisory Verifier. The
Verifier did not replace successful Planner SQL. PV-SQL used the authors'
Probe–Generate–Verify/Repair pipeline at commit
`9f2bcd9a16ec1029649222851bd9e08a2af3e2c6`; each listed model performed probing,
generation and repair. Qwen3 thinking was disabled. All calls used temperature 0,
seed 917, a 16,384-token context limit and a 2,048-token output limit.

This is a paired local configuration comparison. It is not an official leaderboard
submission and should not be compared directly with PV-SQL's published full BIRD
numbers, which use a different dataset split and serving configuration. Raw model
responses and third-party database files are excluded from the repository.
