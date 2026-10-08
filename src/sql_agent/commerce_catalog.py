"""Versioned, explicitly scoped commerce metrics for the local demonstration.

Amounts are integer cents. These references are application-owned business rules,
not hidden benchmark answers. The planner never receives reference SQL.
"""
from .business_context import MetricDefinition, QualityCheck
from .data_agent import DataContract
from .sql_config import SQLTask

REFERENCES = {
    'net_revenue': "SELECT COALESCE((SELECT SUM(amount_cents) FROM orders),0) - COALESCE((SELECT SUM(amount_cents) FROM refunds WHERE status='approved'),0) AS net_revenue_cents",
    'gross_revenue': 'SELECT COALESCE(SUM(amount_cents),0) AS gross_revenue_cents FROM orders',
    'approved_refunds': "SELECT COALESCE(SUM(amount_cents),0) AS approved_refunds_cents FROM refunds WHERE status='approved'",
}
QUESTIONS = {
    'net_revenue': 'What is total net revenue in cents?',
    'gross_revenue': 'What is total gross revenue in cents?',
    'approved_refunds': 'What is the total approved refund amount in cents?',
}


def make_task(metric, path, *, task_id=None, verified=True):
    definition = MetricDefinition('commerce_revenue', ('revenue','refund'),
        'All amounts are integer cents. Gross revenue is the sum of all order amounts. '
        'Approved refunds sum only refunds whose status is approved. Pending refunds are excluded. '
        'Net revenue is gross revenue minus approved refunds. Count each order once; '
        'an order with no approved refunds contributes its full amount. Multiple refund rows '
        'must not multiply order amounts. An empty sum is zero.',
        'SQL-Agent local commerce policy', '1', True)
    checks = (
        QualityCheck('valid_order_amounts', 'SELECT COUNT(*) FROM orders WHERE amount_cents IS NULL OR amount_cents < 0 OR typeof(amount_cents) != \'integer\'', 'violation_count',0,'blocking','Local policy requires nonnegative integer-cent order amounts.'),
        QualityCheck('valid_refunds', "SELECT COUNT(*) FROM refunds WHERE amount_cents IS NULL OR amount_cents < 0 OR typeof(amount_cents) != 'integer' OR status IS NULL OR status NOT IN ('approved','pending')",'violation_count',0,'blocking','Local policy requires valid cents and an explicit refund status.'),
        QualityCheck('refund_references', 'SELECT COUNT(*) FROM refunds r LEFT JOIN orders o ON o.id=r.order_id WHERE o.id IS NULL', 'violation_count',0,'blocking','Every refund must reference a registered order.'),
        QualityCheck('daily_ingestion','SELECT MAX(completed_at) FROM ingestion_log','freshness',24,'blocking','Demo dataset refresh budget: 24 hours; not a general production SLO.'),
    )
    return SQLTask(task_id or metric,QUESTIONS[metric],DataContract((metric+'_cents',),min_rows=1,max_rows=1,
                   verification_sql=REFERENCES[metric] if verified else ''),path,(definition,),checks)
