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
    where not exists (
        select 1 from declared d where d.tenant_id = e.tenant_id
            and d.workflow_id = e.workflow_id and d.run_id = e.run_id
    )
    group by 1, 2, 3
), task_costs as (
    select tenant_id, workflow_id, run_id, task_id,
        count(*) as call_count, sum(cost_amount) as model_cost,
        max(cast(incomplete as integer)) as incomplete_calls,
        count(distinct currency) as currencies,
        count(distinct price_table_version) as price_versions,
        min(currency) as currency,
        count(distinct config_version) as config_versions
    from {{ ref('int_calls') }}
    group by 1, 2, 3, 4
), run_call_versions as (
    select tenant_id, workflow_id, run_id,
        count(distinct price_table_version) as price_versions
    from {{ ref('int_calls') }}
    group by 1, 2, 3
), event_compat as (
    select e.tenant_id, e.workflow_id, e.run_id, e.task_id,
        max(cast(e.identity_conflict as integer)) as identity_conflict,
        max(cast(e.workflow_version is distinct from json_extract_string(d.document_json, '$.workflow_version')
            or e.experiment_version is distinct from json_extract_string(d.document_json, '$.experiment_version')
            or e.cohort_version is distinct from json_extract_string(d.document_json, '$.cohort_version')
            or e.config_version is distinct from json_extract_string(d.document_json, '$.config_version')
            or e.code_revision is distinct from json_extract_string(d.document_json, '$.code_revision')
            or e.dataset_version is distinct from json_extract_string(d.document_json, '$.dataset_version')
            or e.simulated is distinct from cast(json_extract_string(d.document_json, '$.dataset_simulated') as boolean)
            as integer)) as version_mismatch
    from {{ ref('stg_events') }} e
    join declaration d using (tenant_id, workflow_id, run_id)
    group by 1, 2, 3, 4
), task_rows as (
    select t.tenant_id, t.workflow_id, t.run_id, t.task_id,
        t.first_started_at, t.terminal_at, t.terminal_status, t.latency_ms,
        o.status as outcome_status, o.correct, o.review_cost, o.error_cost,
        o.currency as outcome_currency, o.incomplete as outcome_incomplete,
        c.call_count, c.model_cost, c.currency as call_currency,
        c.incomplete_calls, c.currencies as call_currencies,
        c.price_versions, c.config_versions,
        coalesce(v.identity_conflict, 0) as identity_conflict,
        coalesce(v.version_mismatch, 0) as version_mismatch,
        (t.incomplete or coalesce(o.incomplete, false)
            or coalesce(c.incomplete_calls, 0) > 0
            or coalesce(c.currencies, 0) > 1
            or coalesce(c.price_versions, 0) > 1
            or coalesce(c.config_versions, 0) > 1
            or (c.currency is not null and o.currency is not null
                and c.currency <> o.currency)
            or coalesce(v.identity_conflict, 0) > 0
            or coalesce(v.version_mismatch, 0) > 0) as incomplete
    from {{ ref('int_tasks') }} t
    left join {{ ref('int_outcomes') }} o using (tenant_id, workflow_id, run_id, task_id)
    left join task_costs c using (tenant_id, workflow_id, run_id, task_id)
    left join event_compat v using (tenant_id, workflow_id, run_id, task_id)
), run_rollup as (
    select tenant_id, workflow_id, run_id,
        count(*) as expected_tasks,
        count(*) filter (where first_started_at is not null) as received_tasks,
        count(*) filter (where terminal_status = 'completed') as completed_tasks,
        count(*) filter (where terminal_status = 'failed') as failed_tasks,
        count(*) filter (where outcome_status = 'observed' and correct = true
            and not incomplete) as correct_tasks,
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
), evaluated_checks as (
    select x.tenant_id, x.workflow_id, x.run_id, x.suite_id, x.case_id,
        count(*) as required_checks,
        count(*) filter (where e.status = 'pass' and not e.incomplete) as passed_checks,
        count(*) filter (where e.status = 'error') as error_checks,
        count(*) filter (where e.status is null) as missing_checks,
        count(*) filter (where e.incomplete) as conflicting_checks
    from expected_checks x
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
    r.missing_outcomes,
    case when r.incomplete_calls > 0 or r.call_currencies > 1
        or coalesce(cv.price_versions, 0) > 1 or r.any_incomplete > 0
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
    (d.versions = 1 and r.received_tasks = r.expected_tasks
        and r.missing_outcomes = 0 and r.any_incomplete = 0
        and r.call_currencies <= 1 and r.outcome_currencies <= 1
        and coalesce(cv.price_versions, 0) <= 1
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
left join run_call_versions cv using (tenant_id, workflow_id, run_id)
