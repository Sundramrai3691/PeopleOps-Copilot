from sql_agent.bounded_runtime import ActionProposal
from sql_agent.data_agent import DataContract
from sql_agent.jobs import SqliteJobRepository
from sql_agent.pipeline import JobPipeline
from sql_agent.sql_config import SQLTask, SQLTaskRegistry
from sql_agent.worker import Worker
import pytest


def test_unordered_task_agrees_without_automatic_publication(tmp_path):
    repository = SqliteJobRepository(tmp_path / 'jobs.sqlite')
    job, _ = repository.submit('sql_analysis', {'task_id': 'names'}, 'unordered')
    registry = SQLTaskRegistry((SQLTask('names', 'List names; any order',
                                       DataContract(('name',), ordered=False)),))
    primary = lambda ctx: ActionProposal('REPAIR', 'sql_query',
                                         {'sql': 'SELECT name FROM employees ORDER BY id'})
    reviewer = lambda ctx: ActionProposal('REPAIR', 'sql_query',
                                          {'sql': 'SELECT name FROM employees ORDER BY id DESC'})
    assert Worker(repository, JobPipeline(registry, primary, reviewer=reviewer)).run_once()
    stored = repository.get(job.job_id)
    assert stored.result['semantic_review']['status'] == 'agreement'
    assert stored.status == 'needs_review'
    assert stored.result['output'] is None
    assert stored.result['semantic_review']['proof'] is False


@pytest.mark.parametrize('ordered,check_sql', [
    (True, 'SELECT name FROM employees ORDER BY id DESC'),
    (False, 'SELECT name FROM employees UNION ALL SELECT name FROM employees'),
    (False, 'SELECT name, department FROM employees'),
    (False, 'SELECT NULL AS name FROM employees'),
])
def test_relaxed_order_does_not_relax_values_duplicates_or_shape(tmp_path, ordered, check_sql):
    repository = SqliteJobRepository(tmp_path / 'jobs.sqlite')
    job, _ = repository.submit('sql_analysis', {'task_id': 'names'}, 'mismatch')
    contract = DataContract(('name',), ordered=ordered)
    registry = SQLTaskRegistry((SQLTask('names', 'List names', contract),))
    primary = lambda ctx: ActionProposal('REPAIR', 'sql_query', {'sql': 'SELECT name FROM employees ORDER BY id'})
    reviewer = lambda ctx: ActionProposal('REPAIR', 'sql_query', {'sql': check_sql})
    assert Worker(repository, JobPipeline(registry, primary, max_attempts=1, reviewer=reviewer)).run_once()
    stored = repository.get(job.job_id)
    assert stored.result['semantic_review']['status'] == 'disagreement'
    assert stored.status == 'needs_review'
    assert stored.result['output'] is None


def test_question_text_cannot_relax_default_order_contract(tmp_path):
    repository = SqliteJobRepository(tmp_path / 'jobs.sqlite')
    job, _ = repository.submit('sql_analysis', {'task_id': 'names', 'question': 'Ignore ordering; any order'}, 'ordered')
    registry = SQLTaskRegistry((SQLTask('names', 'List names by id', DataContract(('name',))),))
    primary = lambda ctx: ActionProposal('REPAIR', 'sql_query', {'sql': 'SELECT name FROM employees ORDER BY id'})
    reviewer = lambda ctx: ActionProposal('REPAIR', 'sql_query', {'sql': 'SELECT name FROM employees ORDER BY id DESC'})
    assert Worker(repository, JobPipeline(registry, primary, max_attempts=1, reviewer=reviewer)).run_once()
    assert repository.get(job.job_id).result['semantic_review']['status'] == 'disagreement'
