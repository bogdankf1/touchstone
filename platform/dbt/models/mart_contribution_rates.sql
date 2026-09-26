with declarations as (
    select tenant_id, workflow_id, run_id, min(document_json) as document_json
    from {{ source('staging', 'raw_declarations') }}
    group by 1, 2, 3
), expected_members as (
    select d.tenant_id, d.workflow_id, d.run_id,
        json_extract_string(m.value, '$.metric_id') as metric_id,
        json_extract_string(m.value, '$.definition_version') as definition_version,
        json_extract_string(m.value, '$.unit') as unit,
        json_extract_string(t.value, '$') as task_id
    from declarations d, json_each(d.document_json, '$.metric_expectations') m,
        json_each(m.value, '$.expected_task_ids') t
), expected as (
    select tenant_id, workflow_id, run_id, metric_id, definition_version, unit,
        count(distinct task_id) as expected_tasks
    from expected_members
    group by 1, 2, 3, 4, 5, 6
), totals as (
    select tenant_id, workflow_id, run_id, metric_id, definition_version, unit,
        count(*) as task_contributions,
        count(*) filter (where incomplete) as incomplete_contributions,
        sum(numerator) as numerator_sum,
        sum(denominator) as denominator_sum
    from {{ ref('mart_contributions') }}
    group by 1, 2, 3, 4, 5, 6
), unexpected as (
    select c.tenant_id, c.workflow_id, c.run_id, c.metric_id,
        c.definition_version, c.unit, count(*) as unexpected_tasks
    from {{ ref('mart_contributions') }} c
    left join expected_members e using (
        tenant_id, workflow_id, run_id, metric_id, definition_version, unit, task_id
    )
    where e.task_id is null
    group by 1, 2, 3, 4, 5, 6
), joined as (
    select coalesce(t.tenant_id, e.tenant_id) as tenant_id,
        coalesce(t.workflow_id, e.workflow_id) as workflow_id,
        coalesce(t.run_id, e.run_id) as run_id,
        coalesce(t.metric_id, e.metric_id) as metric_id,
        coalesce(t.definition_version, e.definition_version) as definition_version,
        coalesce(t.unit, e.unit) as unit,
        e.expected_tasks, coalesce(t.task_contributions, 0) as task_contributions,
        coalesce(t.incomplete_contributions, 0) as incomplete_contributions,
        coalesce(u.unexpected_tasks, 0) as unexpected_tasks,
        t.numerator_sum, t.denominator_sum
    from totals t
    full outer join expected e using (
        tenant_id, workflow_id, run_id, metric_id, definition_version, unit
    )
    left join unexpected u on u.tenant_id = coalesce(t.tenant_id, e.tenant_id)
        and u.workflow_id = coalesce(t.workflow_id, e.workflow_id)
        and u.run_id = coalesce(t.run_id, e.run_id)
        and u.metric_id = coalesce(t.metric_id, e.metric_id)
        and u.definition_version = coalesce(t.definition_version, e.definition_version)
        and u.unit = coalesce(t.unit, e.unit)
)
select j.*, r.declared_at,
    j.expected_tasks is not null as coverage_known,
    case when j.expected_tasks is not null
        then j.expected_tasks - j.task_contributions + j.unexpected_tasks end as missing_tasks,
    case when j.expected_tasks = j.task_contributions
        and j.unexpected_tasks = 0 and j.incomplete_contributions = 0
        then j.numerator_sum end as eligible_numerator,
    case when j.expected_tasks = j.task_contributions
        and j.unexpected_tasks = 0 and j.incomplete_contributions = 0
        then j.denominator_sum end as eligible_denominator,
    case when j.expected_tasks = j.task_contributions
        and j.unexpected_tasks = 0 and j.incomplete_contributions = 0
        and j.denominator_sum <> 0
        then j.numerator_sum / j.denominator_sum end as rate
from joined j
left join {{ ref('mart_runs') }} r using (tenant_id, workflow_id, run_id)
