with outcomes as (
    select tenant_id, workflow_id, run_id, task_id,
        count(distinct content_sha256) as versions,
        count(distinct status) as statuses,
        count(distinct correct) as correctness_versions,
        count(distinct review_cost) as review_versions,
        count(distinct error_cost) as error_versions,
        count(distinct currency) as currencies,
        count(distinct outcome_version) as outcome_versions,
        max(cast(identity_conflict as integer)) as identity_conflict,
        min(status) as status, min(correct) as correct,
        min(review_cost) as review_cost, min(error_cost) as error_cost,
        min(currency) as currency, min(outcome_version) as outcome_version
    from {{ ref('stg_events') }}
    where event_kind = 'outcome'
    group by 1, 2, 3, 4
)
select *, (identity_conflict > 0 or status <> 'observed' or correct is null
    or review_cost is null or error_cost is null
    or statuses > 1 or correctness_versions > 1
    or review_versions > 1 or error_versions > 1 or currencies > 1
    or outcome_versions > 1) as incomplete
from outcomes
