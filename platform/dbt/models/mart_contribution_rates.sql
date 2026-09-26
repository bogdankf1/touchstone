with totals as (
    select tenant_id, workflow_id, run_id, metric_id, definition_version, unit,
        count(*) as task_contributions,
        count(*) filter (where incomplete) as incomplete_contributions,
        sum(numerator) as numerator_sum,
        sum(denominator) as denominator_sum
    from {{ ref('mart_contributions') }}
    group by 1, 2, 3, 4, 5, 6
)
select t.tenant_id, t.workflow_id, t.run_id, t.metric_id,
    t.definition_version, t.unit, r.declared_at,
    t.task_contributions, t.incomplete_contributions,
    case when t.incomplete_contributions = 0 then t.numerator_sum end as numerator_sum,
    case when t.incomplete_contributions = 0 then t.denominator_sum end as denominator_sum,
    case when t.incomplete_contributions = 0 and t.denominator_sum <> 0
        then t.numerator_sum / t.denominator_sum end as rate
from totals t
left join {{ ref('mart_runs') }} r using (tenant_id, workflow_id, run_id)
