with calls as (
    select tenant_id, workflow_id, run_id, task_id, call_id,
        count(distinct cast(json_extract(document_json, '$.payload') as varchar)) as versions,
        count(distinct node_name) as nodes,
        count(distinct currency) as currencies,
        count(distinct price_table_version) as price_versions,
        count(distinct config_version) as config_versions,
        count(distinct cost_amount) as amounts,
        max(cast(identity_conflict as integer)) as identity_conflict,
        max(cast(cost_status = 'unavailable' or cost_amount is null as integer)) as unavailable,
        min(currency) as currency, min(price_table_version) as price_table_version,
        min(config_version) as config_version, min(cost_amount) as cost_amount,
        min(node_name) as node_name,
        min(trace_id) as trace_id, min(event_id) as event_id
    from {{ ref('stg_events') }}
    where event_kind = 'provider_usage'
    group by 1, 2, 3, 4, 5
)
select *, (identity_conflict > 0 or versions > 1 or nodes > 1
    or currencies > 1 or price_versions > 1
    or config_versions > 1 or amounts > 1 or unavailable > 0) as incomplete
from calls
