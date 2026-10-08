"""Application-owned SQL tasks and model configuration.

Requests select registered tasks; they cannot choose a database path or weaken
its contract. Without a model endpoint only the explicitly named demo task runs.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3

from .bounded_runtime import ActionProposal
from .data_agent import SQLAgentPlanner, SQLAgentSession, DataContract, PlanningContext
from .planner import OllamaPlannerModel, OpenAICompatiblePlannerModel
from .sql_environment import demo_database
from .business_context import MetricDefinition, QualityCheck, context_hash


@dataclass(frozen=True)
class SQLTask:
    task_id: str
    question: str
    contract: DataContract
    database: Path | None = None

    definitions: tuple[MetricDefinition, ...] = ()
    quality_checks: tuple[QualityCheck, ...] = ()
    example_questions: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'example_questions', tuple(self.example_questions))
        object.__setattr__(self, 'definitions', tuple(self.definitions))
        object.__setattr__(self, 'quality_checks', tuple(self.quality_checks))
        for values, attr in [(self.definitions,'metric_id'), (self.quality_checks,'check_id')]:
            ids=[getattr(v,attr) for v in values]
            if len(ids)!=len(set(ids)):raise ValueError('duplicate context IDs')

    @property
    def context_sha256(self):
        return context_hash(self.definitions,self.quality_checks)

    def validate_question(self, question):
        if self.contract.verification_sql and question and question != self.question:
            raise ValueError('verified task requires its registered question')

    def describe(self) -> dict:
        return {"task_id": self.task_id, "question": self.question,
                "contract": self.contract.snapshot().to_dict(),
                "data_source": "registered_sqlite" if self.database else "synthetic_demo",
                "context_sha256": self.context_sha256,
                "metric_ids": [d.metric_id for d in self.definitions],
                "quality_check_ids": [c.check_id for c in self.quality_checks], "example_questions": list(self.example_questions)}


class SQLTaskRegistry:
    def __init__(self, tasks: tuple[SQLTask, ...] | None = None) -> None:
        tasks = tasks if tasks is not None else (SQLTask(
            "employee_names", "List employee names", DataContract(("name",), non_null=("name",), min_rows=1,
                verification_sql="SELECT name FROM employees ORDER BY id")),)
        self.tasks = {task.task_id: task for task in tasks}
        if not self.tasks or len(self.tasks) != len(tasks):
            raise ValueError("task IDs must be nonempty and unique")

    @classmethod
    def from_env(cls) -> SQLTaskRegistry:
        config = os.environ.get("SQL_AGENT_SQL_TASKS")
        if not config:
            return cls()
        path = Path(config).resolve()
        rows = json.loads(path.read_text())
        tasks = []
        for row in rows:
            database = (path.parent / row["database"]).resolve()
            if not database.is_file():
                raise ValueError(f"registered database does not exist: {row['task_id']}")
            tasks.append(SQLTask(row["task_id"], row["question"], DataContract(**row["contract"]), database,
                tuple(MetricDefinition(**d) for d in row.get('definitions', [])),
                tuple(QualityCheck(**c) for c in row.get('quality_checks', [])), tuple(row.get('example_questions', []))))
        return cls(tuple(tasks))

    def get(self, task_id: str) -> SQLTask:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise ValueError("unregistered SQL task") from exc

    def describe(self) -> list[dict]:
        return [task.describe() for task in self.tasks.values()]

    def session(self, job) -> SQLAgentSession:
        task = self.get(job.payload["task_id"])
        task.validate_question(job.payload.get('question'))
        connection = (sqlite3.connect(task.database.as_uri() + "?mode=ro", uri=True)
                      if task.database else demo_database())
        connection.execute("PRAGMA query_only=ON")
        return SQLAgentSession(connection, task.contract)


class DemoSQLPlanner:
    """A fixed demo policy, explicitly not a natural-language model."""
    def __call__(self, context: PlanningContext) -> ActionProposal:
        if context.goal.strip().lower().rstrip(".") != "list employee names":
            return ActionProposal("STOP", source="scripted_demo_unsupported_goal")
        sql = context.initial_sql if context.attempt == 1 and context.initial_sql else "SELECT name FROM employees ORDER BY id"
        return ActionProposal("REPAIR", "sql_query", {"sql": sql}, "scripted_demo")


def sql_planner_from_env():
    base = os.environ.get("SQL_AGENT_PLANNER_BASE_URL", "").strip()
    model = os.environ.get("SQL_AGENT_PLANNER_MODEL", "").strip()
    if not base and not model:
        if os.environ.get("SQL_AGENT_SQL_TASKS"):
            raise ValueError("registered databases require an explicit model endpoint")
        return DemoSQLPlanner()
    if not base or not model:
        raise ValueError("both planner base URL and model must be configured")
    thinking_value = os.environ.get("SQL_AGENT_PLANNER_THINKING", "false")
    if thinking_value not in {"true", "false"}:
        raise ValueError("thinking must be true or false")
    thinking = thinking_value == "true"
    timeout = float(os.environ.get("SQL_AGENT_PLANNER_TIMEOUT_SECONDS", "120" if thinking else "30"))
    generation_format = os.environ.get('SQL_AGENT_SQL_GENERATION_FORMAT', 'json')
    if generation_format not in {'json', 'sql'}:
        raise ValueError('unsupported SQL generation format')
    provider = os.environ.get("SQL_AGENT_PLANNER_PROVIDER", "ollama")
    if provider == "ollama":
        adapter = OllamaPlannerModel(base, model, timeout=timeout,
            max_tokens=int(os.environ.get('SQL_AGENT_PLANNER_MAX_TOKENS', '8192' if thinking else ('2048' if generation_format == 'sql' else '256'))),
            json_mode=generation_format == 'json', thinking=thinking,
            keep_alive_seconds=(int(os.environ['SQL_AGENT_PLANNER_KEEP_ALIVE_SECONDS'])
                if 'SQL_AGENT_PLANNER_KEEP_ALIVE_SECONDS' in os.environ else None))
    elif provider == "openai_compatible":
        if generation_format != 'json' or thinking:
            raise ValueError('SQL text profile currently requires Ollama')
        adapter = OpenAICompatiblePlannerModel(base, model,
            api_key=os.environ.get("SQL_AGENT_PLANNER_API_KEY", ""), timeout=timeout)
    else:
        raise ValueError("unsupported planner provider")
    if generation_format == 'sql':
        from .native_sql import NativeSQLPlanner
        return NativeSQLPlanner(adapter, semantic_guidance=True)
    return SQLAgentPlanner(adapter)
