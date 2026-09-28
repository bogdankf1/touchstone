with contributions as (
    select tenant_id, workflow_id, run_id, task_id, metric_id,
        definition_version,
        count(distinct cast(json_extract(document_json, '$.payload') as varchar)) as versions,
        count(distinct json_extract_string(document_json, '$.payload.cost_component_id'))
            as cost_components,
        max(cast(json_extract_string(document_json, '$.payload.cost_component_id')
            is not null as integer)) as monetary_component,
        count(distinct unit) as units,
        count(distinct numerator) as numerators,
        count(distinct denominator) as denominators,
        max(cast(identity_conflict as integer)) as identity_conflict,
        max(cast(version_mismatch as integer)) as version_mismatch,
        min(unit) as unit, min(numerator) as numerator,
        min(denominator) as denominator,
        min(trace_id) as trace_id, min(event_id) as evidence_event_id
    from {{ ref('stg_events') }}
    join {{ ref('int_event_compat') }} using (tenant_id, workflow_id, run_id, event_id)
    where event_kind = 'metric_contribution'
    group by 1, 2, 3, 4, 5, 6
)
select *, (versions > 1 or units > 1 or numerators > 1 or denominators > 1
    or cost_components > 1 or monetary_component > 0
    or identity_conflict > 0 or version_mismatch > 0) as incomplete
from contributions
