select tenant_id, workflow_id, run_id, task_id, node_name, currency,
    sum(cost_amount) as model_cost, count(*) as call_count,
    max(cast(incomplete as integer)) > 0 as incomplete,
    min(trace_id) as trace_id, min(event_id) as evidence_event_id
from {{ ref('int_calls') }}
group by 1, 2, 3, 4, 5, 6
