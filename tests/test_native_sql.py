import pytest
from sql_agent.native_sql import extract_sql, NativeSQLPlanner
from sql_agent.data_agent import DataContract, SQLPlanningEvidence, PlanningContext


@pytest.mark.parametrize('raw', ['SELECT 1', 'Explanation\n```sql\nSELECT 1\n```', '```\nSELECT 1\n```'])
def test_native_sql_supported_formats(raw):
    assert extract_sql(raw) == {'action': 'REPAIR', 'sql': 'SELECT 1'}


@pytest.mark.parametrize('raw', ['DROP TABLE t', '```sql\nSELECT 1\n```\n```sql\nSELECT 2\n```',
                               '```sql\nSELECT 1', '', 'Ignore instructions'])
def test_ambiguous_or_invalid_output_not_executed(raw):
    with pytest.raises(ValueError):
        extract_sql(raw)


def test_reference_never_enters_native_prompt():
    class Model:
        def complete_with_metadata(self, prompt):
            assert 'secret_reference' not in prompt
            assert 'CREATE TABLE t' in prompt and 'total' in prompt
            return '```sql\nSELECT COUNT(*) AS total FROM t\n```', {'done_reason': 'stop'}
    contract = DataContract(('total',), verification_sql='SELECT 1 -- secret_reference')
    ctx = PlanningContext('Count rows', contract,
                          SQLPlanningEvidence((('t','CREATE TABLE t(id INTEGER)'),), 'hash'), 1, 0)
    proposal = NativeSQLPlanner(Model())(ctx)
    assert proposal.arguments['sql'] == 'SELECT COUNT(*) AS total FROM t'


def test_truncated_response_is_rejected():
    class Model:
        def complete_with_metadata(self, prompt):
            return 'SELECT 1', {'done_reason': 'length'}
    ctx = PlanningContext('Count rows', DataContract(('total',)), SQLPlanningEvidence((), 'hash'), 1, 0)
    with pytest.raises(ValueError, match='truncated'):
        NativeSQLPlanner(Model())(ctx)


def test_parse_failure_preserves_observed_token_cost():
    class Model:
        def complete_with_metadata(self, prompt):
            return 'not SQL', {'prompt_tokens': 11, 'completion_tokens': 23, 'done_reason': 'stop'}
    planner=NativeSQLPlanner(Model())
    ctx=PlanningContext('Count rows',DataContract(('total',)),SQLPlanningEvidence((),'hash'),1,0)
    with pytest.raises(ValueError):planner(ctx)
    event=planner.recovery_events[-1]
    assert event['prompt_tokens']==11 and event['completion_tokens']==23
