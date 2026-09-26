with declarations as (
    select tenant_id, workflow_id, run_id,
        min(document_json) as document_json,
        count(distinct content_sha256) as declaration_versions
    from {{ source('staging', 'raw_declarations') }}
    group by 1, 2, 3
), expected as (
    select d.tenant_id, d.workflow_id, d.run_id,
        json_extract_string(j.value, '$') as task_id,
        d.document_json as declaration_json, d.declaration_versions
    from declarations d, json_each(d.document_json, '$.expected_task_ids') j
), roots as (
    select tenant_id, workflow_id, run_id, task_id,
        min(cast(started_at as timestamptz)) as first_started_at,
        max(cast(ended_at as timestamptz)) as terminal_at,
        max(attempt_number) as last_attempt,
        max(duration_ms) as single_attempt_duration_ms,
        max(cast(identity_conflict as integer)) as identity_conflict,
        count(distinct config_version) as config_versions,
        count(distinct cohort_version) as cohort_versions,
        count(distinct experiment_version) as experiment_versions,
        count(distinct workflow_version) as workflow_versions,
        count(distinct simulated) as simulation_versions
    from {{ ref('stg_events') }}
    where event_kind = 'execution' and parent_task_id is null
    group by 1, 2, 3, 4
), terminal as (
    select e.tenant_id, e.workflow_id, e.run_id, e.task_id,
        count(distinct e.status) as terminal_versions,
        min(e.status) as terminal_status
    from {{ ref('stg_events') }} e
    join roots r using (tenant_id, workflow_id, run_id, task_id)
    where e.event_kind = 'execution' and e.parent_task_id is null
        and e.attempt_number = r.last_attempt and e.status in ('completed', 'failed')
    group by 1, 2, 3, 4
)
select x.tenant_id, x.workflow_id, x.run_id, x.task_id,
    x.declaration_json, x.declaration_versions, r.first_started_at, r.terminal_at,
    r.last_attempt, t.terminal_status,
    case when r.first_started_at is not null and r.terminal_at is not null
        and t.terminal_versions = 1
        then case when r.last_attempt = 1 then r.single_attempt_duration_ms
            else date_diff('microsecond', r.first_started_at, r.terminal_at) / 1000.0
            end
        end as latency_ms,
    (x.declaration_versions > 1 or coalesce(r.identity_conflict, 0) > 0
        or coalesce(r.config_versions, 0) > 1 or coalesce(r.cohort_versions, 0) > 1
        or coalesce(r.experiment_versions, 0) > 1 or coalesce(r.workflow_versions, 0) > 1
        or coalesce(r.simulation_versions, 0) > 1
        or coalesce(t.terminal_versions, 0) > 1) as incomplete
from expected x
left join roots r using (tenant_id, workflow_id, run_id, task_id)
left join terminal t using (tenant_id, workflow_id, run_id, task_id)
