with identities as (
    select tenant_id, workflow_id, run_id, event_id,
        count(distinct content_sha256) as identity_versions
    from {{ source('staging', 'raw_measurements') }}
    group by 1, 2, 3, 4
), ranked as (
    select m.*, i.identity_versions,
        row_number() over (
            partition by m.tenant_id, m.workflow_id, m.run_id, m.event_id
            order by m.content_sha256, m.received_at
        ) as row_num
    from {{ source('staging', 'raw_measurements') }} m
    join identities i using (tenant_id, workflow_id, run_id, event_id)
)
select tenant_id, workflow_id, run_id, task_id, event_id, event_kind, node_name,
    trace_id, span_id, received_at, content_sha256, document_json,
    cost_amount, review_cost, error_cost, identity_versions > 1 as identity_conflict,
    json_extract_string(document_json, '$.workflow_version') as workflow_version,
    json_extract_string(document_json, '$.reproducibility.experiment_version') as experiment_version,
    json_extract_string(document_json, '$.reproducibility.cohort_version') as cohort_version,
    json_extract_string(document_json, '$.reproducibility.config_version') as config_version,
    json_extract_string(document_json, '$.reproducibility.code_revision') as code_revision,
    json_extract_string(document_json, '$.reproducibility.dataset_version') as dataset_version,
    cast(json_extract_string(document_json, '$.simulated') as boolean) as simulated,
    json_extract_string(document_json, '$.payload.currency') as currency,
    json_extract_string(document_json, '$.payload.price_table_version') as price_table_version,
    json_extract_string(document_json, '$.payload.call_id') as call_id,
    json_extract_string(document_json, '$.payload.cost_status') as cost_status,
    json_extract_string(document_json, '$.payload.status') as status,
    cast(json_extract_string(document_json, '$.payload.correct') as boolean) as correct,
    json_extract_string(document_json, '$.payload.outcome_version') as outcome_version,
    json_extract_string(document_json, '$.payload.started_at') as started_at,
    json_extract_string(document_json, '$.payload.ended_at') as ended_at,
    cast(json_extract_string(document_json, '$.payload.duration_ms') as double) as duration_ms,
    cast(json_extract_string(document_json, '$.payload.attempt_number') as integer) as attempt_number,
    json_extract_string(document_json, '$.payload.parent_task_id') as parent_task_id,
    json_extract_string(document_json, '$.payload.suite_id') as suite_id,
    json_extract_string(document_json, '$.payload.case_id') as case_id,
    json_extract_string(document_json, '$.payload.metric_id') as metric_id,
    json_extract_string(document_json, '$.payload.definition_version') as definition_version,
    cast(json_extract_string(document_json, '$.payload.numerator') as double) as numerator,
    cast(json_extract_string(document_json, '$.payload.denominator') as double) as denominator,
    json_extract_string(document_json, '$.payload.unit') as unit
from ranked
where row_num = 1
