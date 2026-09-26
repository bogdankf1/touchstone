with checks as (
    select tenant_id, workflow_id, run_id, suite_id, case_id, metric_id,
        count(distinct status) as status_versions,
        count(distinct cast(json_extract(document_json, '$.payload') as varchar)) as versions,
        max(cast(identity_conflict as integer)) as identity_conflict,
        min(status) as status,
        min(trace_id) as trace_id, min(event_id) as event_id
    from {{ ref('stg_events') }}
    where event_kind = 'evaluation'
    group by 1, 2, 3, 4, 5, 6
)
select *, (versions > 1 or status_versions > 1 or identity_conflict > 0) as incomplete
from checks
