with costs as (
    select c.tenant_id, c.workflow_id, c.run_id, coalesce(m.root_task_id, c.task_id) as task_id,
        c.node_name, min(c.currency) as currency,
        count(distinct c.currency) as currencies,
        sum(case when c.cost_scope='online' then c.cost_amount else 0 end) as online_cost,
        sum(case when c.cost_scope='offline' then c.cost_amount else 0 end) as offline_cost,
        bool_or(c.cost_scope='online' and c.cost_amount is null) as online_unknown,
        bool_or(c.cost_scope='offline' and c.cost_amount is null) as offline_unknown,
        bool_and(coalesce(o.complete,true)) as online_cost_complete,
        bool_and(coalesce(f.complete,true)) as offline_cost_complete,
        count(*) as call_count, bool_or(c.incomplete) as incomplete,
        min(c.trace_id) as trace_id, min(c.event_id) as evidence_event_id
    from {{ ref('int_calls') }} c
    left join {{ ref('int_task_membership') }} m using (tenant_id, workflow_id, run_id, task_id)
    left join {{ ref('int_cost_scopes') }} o
        on (c.tenant_id,c.workflow_id,c.run_id)=(o.tenant_id,o.workflow_id,o.run_id)
        and o.cost_scope='online'
    left join {{ ref('int_cost_scopes') }} f
        on (c.tenant_id,c.workflow_id,c.run_id)=(f.tenant_id,f.workflow_id,f.run_id)
        and f.cost_scope='offline'
    group by 1, 2, 3, 4, 5
)
-- Node amounts are observed evidence: run-level scope completeness withholds run totals
-- and provider spend, never a node's observed cost. An unknown amount is never zero, and
-- amounts in different currencies are never summed under one currency label.
select * exclude (online_cost,offline_cost,currencies,online_unknown,offline_unknown),
    case when not online_unknown and currencies <= 1 then online_cost end as model_cost,
    case when not offline_unknown and currencies <= 1 then offline_cost end as offline_model_cost,
    case when online_cost_complete and offline_cost_complete and currencies <= 1
        then online_cost + offline_cost end as provider_spend
from costs
