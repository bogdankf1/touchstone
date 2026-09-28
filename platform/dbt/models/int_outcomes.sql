with outcomes as (
    select tenant_id, workflow_id, run_id, task_id,
        count(*) filter (where status = 'observed') as observed_rows,
        count(distinct cast(json_extract(document_json, '$.payload') as varchar))
            filter (where status = 'observed') as observed_versions,
        max(cast(identity_conflict as integer)) as identity_conflict,
        max(cast(version_mismatch as integer)) as version_mismatch,
        min(status) filter (where status = 'observed') as observed_status,
        min(status) as provisional_status,
        min(correct) filter (where status = 'observed') as correct,
        min(review_cost) filter (where status = 'observed') as review_cost,
        min(error_cost) filter (where status = 'observed') as error_cost,
        min(currency) filter (where status = 'observed') as currency,
        min(outcome_version) filter (where status = 'observed') as outcome_version
    from {{ ref('stg_events') }}
    join {{ ref('int_event_compat') }} using (tenant_id, workflow_id, run_id, event_id)
    where event_kind = 'outcome'
    group by 1, 2, 3, 4
)
select tenant_id, workflow_id, run_id, task_id,
    case when observed_rows > 0 then observed_status else provisional_status end as status,
    correct, review_cost, error_cost, currency, outcome_version,
    (identity_conflict > 0 or version_mismatch > 0 or observed_versions > 1 or observed_rows = 0
        or correct is null or review_cost is null or error_cost is null) as incomplete
from outcomes
