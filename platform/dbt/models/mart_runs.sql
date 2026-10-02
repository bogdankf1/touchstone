with declared as (
    select tenant_id, workflow_id, run_id, min(document_json) as document_json,
        count(distinct content_sha256) as versions
    from {{ source('staging', 'raw_declarations') }}
    group by 1, 2, 3
), declaration as (
    select * from declared
    union all
    select e.tenant_id, e.workflow_id, e.run_id,
        cast(null as json) as document_json, 0 as versions
    from {{ ref('stg_events') }} e
    where e.event_kind <> 'comparison_attestation' and not exists (
        select 1 from declared d where d.tenant_id = e.tenant_id
            and d.workflow_id = e.workflow_id and d.run_id = e.run_id
    )
    group by 1, 2, 3
), task_costs as (
    select tenant_id, workflow_id, run_id, root_task_id as task_id,
        count(*) as call_count, sum(cost_amount) as model_cost,
        max(cast(incomplete as integer)) as incomplete_calls,
        count(distinct currency) as currencies,
        min(currency) as currency,
        count(distinct config_version) as config_versions
    from {{ ref('int_calls') }}
    join {{ ref('int_task_membership') }} using (tenant_id, workflow_id, run_id, task_id)
    where cost_scope = 'online'
    group by 1, 2, 3, 4
), rejected_costs as (
    select tenant_id, workflow_id, run_id, task_id, count(*) as rejected_calls
    from {{ source('staging', 'raw_rejections') }}
    where workflow_id is not null and run_id is not null and task_id is not null
        and event_name = 'provider_usage'
    group by 1, 2, 3, 4
), event_compat as (
    select e.tenant_id, e.workflow_id, e.run_id, e.task_id,
        max(cast(e.identity_conflict as integer)) as identity_conflict,
        max(cast(v.version_mismatch as integer)) as version_mismatch
    from {{ ref('stg_events') }} e
    join {{ ref('int_event_compat') }} v using (tenant_id, workflow_id, run_id, event_id)
    where e.event_kind <> 'comparison_attestation'
        and (e.event_kind <> 'provider_usage' or e.cost_scope = 'online')
    group by 1, 2, 3, 4
), run_evidence as (
    select e.tenant_id, e.workflow_id, e.run_id,
        count(distinct e.task_id) filter (where m.task_id is null) as unexpected_tasks,
        max(cast(e.identity_conflict or v.version_mismatch as integer)) as incomplete
    from {{ ref('stg_events') }} e
    join {{ ref('int_event_compat') }} v using (tenant_id, workflow_id, run_id, event_id)
    left join {{ ref('int_task_membership') }} m using (tenant_id, workflow_id, run_id, task_id)
    where e.event_kind <> 'comparison_attestation'
    group by 1, 2, 3
), run_rejections as (
    select tenant_id, workflow_id, run_id, count(*) as rejected_calls
    from {{ source('staging', 'raw_rejections') }}
    where event_name = 'provider_usage'
    group by 1, 2, 3
), task_rows as (
    select t.tenant_id, t.workflow_id, t.run_id, t.task_id,
        t.first_started_at, t.terminal_at, t.terminal_status, t.latency_ms,
        o.status as outcome_status, o.correct, o.review_cost, o.error_cost,
        o.currency as outcome_currency, o.incomplete as outcome_incomplete,
        c.call_count, c.model_cost, c.currency as call_currency,
        c.incomplete_calls, c.currencies as call_currencies,
        c.config_versions,
        coalesce(v.identity_conflict, 0) as identity_conflict,
        coalesce(v.version_mismatch, 0) as version_mismatch,
        (t.incomplete or coalesce(o.incomplete, false)
            or coalesce(c.incomplete_calls, 0) > 0
            or coalesce(c.currencies, 0) > 1
            or coalesce(c.config_versions, 0) > 1
            or (c.currency is not null and o.currency is not null
                and c.currency <> o.currency)
            or coalesce(j.rejected_calls, 0) > 0
            or coalesce(v.identity_conflict, 0) > 0
            or coalesce(v.version_mismatch, 0) > 0) as incomplete
    from {{ ref('int_tasks') }} t
    left join {{ ref('int_outcomes') }} o using (tenant_id, workflow_id, run_id, task_id)
    left join task_costs c using (tenant_id, workflow_id, run_id, task_id)
    left join rejected_costs j using (tenant_id, workflow_id, run_id, task_id)
    left join event_compat v using (tenant_id, workflow_id, run_id, task_id)
), run_rollup as (
    select tenant_id, workflow_id, run_id,
        count(*) as expected_tasks,
        count(*) filter (where first_started_at is not null) as received_tasks,
        count(*) filter (where terminal_status = 'completed') as completed_tasks,
        count(*) filter (where terminal_status = 'failed') as failed_tasks,
        case when count(*) filter (where outcome_status is null or outcome_incomplete) = 0
            then count(*) filter (where outcome_status = 'observed' and correct = true)
            end as correct_tasks,
        count(*) filter (where outcome_status is null) as missing_outcomes,
        count(*) filter (where outcome_status is null or outcome_incomplete)
            as incomplete_outcomes,
        max(coalesce(incomplete_calls, 0)) as incomplete_calls,
        sum(coalesce(model_cost, cast(0 as decimal(38,12)))) as model_cost,
        sum(coalesce(review_cost, cast(0 as decimal(38,12)))) as review_cost,
        sum(coalesce(error_cost, cast(0 as decimal(38,12)))) as error_cost,
        count(distinct call_currency) as call_currencies,
        count(distinct outcome_currency) as outcome_currencies,
        min(coalesce(call_currency, outcome_currency)) as currency,
        max(cast(incomplete as integer)) as any_incomplete,
        count(*) filter (where terminal_status = 'completed' and latency_ms is not null)
            as latency_population,
        quantile_disc(latency_ms, 0.99) filter (
            where terminal_status = 'completed' and latency_ms is not null) as latency_p99_ms
    from task_rows
    group by 1, 2, 3
), expected_checks as (
    select d.tenant_id, d.workflow_id, d.run_id,
        json_extract_string(s.value, '$.suite_id') as suite_id,
        json_extract_string(c.value, '$') as case_id,
        json_extract_string(k.value, '$') as metric_id
    from declaration d, json_each(d.document_json, '$.evaluation_suites') s,
        json_each(s.value, '$.expected_case_ids') c,
        json_each(s.value, '$.required_checks') k
) , late_checks as (
    select w.tenant_id,w.workflow_id,w.run_id,
        json_extract_string(s.value,'$.suite_id') as suite_id,
        json_extract_string(c.value,'$') as case_id,
        json_extract_string(k.value,'$') as metric_id
    from {{ ref('int_work') }} w,
        json_each(w.declaration_json,'$.payload.evaluation_suites') s,
        json_each(s.value,'$.expected_case_ids') c,
        json_each(s.value,'$.required_checks') k
    where w.cost_scope = 'online' and w.declared
), all_checks as (
    select * from expected_checks union select * from late_checks
), evaluated_checks as (
    select x.tenant_id, x.workflow_id, x.run_id, x.suite_id, x.case_id,
        count(*) as required_checks,
        count(*) filter (where e.status = 'pass' and not e.incomplete) as passed_checks,
        count(*) filter (where e.status = 'error') as error_checks,
        count(*) filter (where e.status is null) as missing_checks,
        count(*) filter (where e.incomplete) as conflicting_checks
    from all_checks x
    left join {{ ref('int_evaluations') }} e
        on x.tenant_id = e.tenant_id and x.workflow_id = e.workflow_id
        and x.run_id = e.run_id and x.suite_id = e.suite_id
        and x.case_id = e.case_id and x.metric_id = e.metric_id
    group by 1, 2, 3, 4, 5
), evaluation_rollup as (
    select tenant_id, workflow_id, run_id, count(*) as expected_cases,
        count(*) filter (where passed_checks = required_checks) as passed_cases,
        sum(missing_checks) as missing_checks,
        sum(error_checks) as error_checks,
        sum(conflicting_checks) as conflicting_checks
    from evaluated_checks group by 1, 2, 3
)
, legacy as (
select d.tenant_id, d.workflow_id, d.run_id,
    json_extract_string(d.document_json, '$.workflow_version') as workflow_version,
    json_extract_string(d.document_json, '$.experiment_version') as experiment_version,
    json_extract_string(d.document_json, '$.cohort_version') as cohort_version,
    json_extract_string(d.document_json, '$.config_version') as config_version,
    json_extract_string(d.document_json, '$.measurement_mode') as measurement_mode,
    cast(json_extract_string(d.document_json, '$.dataset_simulated') as boolean) as dataset_simulated,
    cast(json_extract_string(d.document_json, '$.declared_at') as timestamptz) as declared_at,
    d.versions > 0 as completeness_known,
    r.expected_tasks, r.received_tasks, r.completed_tasks, r.failed_tasks,
    r.expected_tasks - r.received_tasks as missing_tasks, r.correct_tasks,
    r.missing_outcomes, coalesce(re.unexpected_tasks, 0) as unexpected_tasks,
    case when r.incomplete_calls > 0 or r.call_currencies > 1
        or r.any_incomplete > 0
        or coalesce(re.unexpected_tasks, 0) > 0 or coalesce(re.incomplete, 0) > 0
        or coalesce(rj.rejected_calls, 0) > 0
        then null else r.model_cost end as model_cost,
    case when r.incomplete_outcomes > 0 or r.outcome_currencies > 1
        then null else r.review_cost end as review_cost,
    case when r.incomplete_outcomes > 0 or r.outcome_currencies > 1
        then null else r.error_cost end as error_cost,
    case when r.call_currencies > 1 or r.outcome_currencies > 1
        then null else r.currency end as currency,
    r.latency_population, r.latency_p99_ms,
    coalesce(e.expected_cases, 0) as expected_cases,
    coalesce(e.passed_cases, 0) as passed_cases,
    coalesce(e.missing_checks, 0) as missing_checks,
    coalesce(e.error_checks, 0) as error_checks,
    coalesce(e.conflicting_checks, 0) as conflicting_checks,
    (d.versions = 1 and coalesce(re.unexpected_tasks, 0) = 0
        and coalesce(re.incomplete, 0) = 0 and coalesce(rj.rejected_calls, 0) = 0
        and r.received_tasks = r.expected_tasks
        and r.completed_tasks + r.failed_tasks = r.expected_tasks
        and r.missing_outcomes = 0 and r.any_incomplete = 0
        and r.call_currencies <= 1 and r.outcome_currencies <= 1
        and (r.call_currencies = 0 or r.outcome_currencies = 0
            or (select min(call_currency) from task_rows t
                where t.tenant_id = d.tenant_id and t.workflow_id = d.workflow_id
                    and t.run_id = d.run_id) =
                (select min(outcome_currency) from task_rows t
                where t.tenant_id = d.tenant_id and t.workflow_id = d.workflow_id
                    and t.run_id = d.run_id))
        and coalesce(e.missing_checks, 0) = 0
        and coalesce(e.conflicting_checks, 0) = 0) as metrics_complete
from declaration d
left join run_rollup r using (tenant_id, workflow_id, run_id)
left join evaluation_rollup e using (tenant_id, workflow_id, run_id)
left join run_evidence re using (tenant_id, workflow_id, run_id)
left join run_rejections rj using (tenant_id, workflow_id, run_id)

)
select l.* exclude (model_cost, metrics_complete),
    case when o.complete and l.model_cost is not null then o.observed_cost end as model_cost,
    coalesce(o.complete and l.model_cost is not null, false) as online_cost_complete,
    coalesce(f.complete, false) as offline_cost_complete,
    case when f.complete then f.observed_cost end as offline_model_cost,
    case when o.complete and f.complete and l.model_cost is not null
        and (o.currency is null or f.currency is null or o.currency = f.currency)
        then o.observed_cost + f.observed_cost end as provider_spend,
    coalesce(l.metrics_complete and o.complete, false) as metrics_complete
from legacy l
left join {{ ref('int_cost_scopes') }} o
    on (l.tenant_id,l.workflow_id,l.run_id) = (o.tenant_id,o.workflow_id,o.run_id)
    and o.cost_scope = 'online'
left join {{ ref('int_cost_scopes') }} f
    on (l.tenant_id,l.workflow_id,l.run_id) = (f.tenant_id,f.workflow_id,f.run_id)
    and f.cost_scope = 'offline'
