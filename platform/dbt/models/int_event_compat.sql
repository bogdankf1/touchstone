with declarations as (
    select tenant_id, workflow_id, run_id, min(document_json) as document_json,
        count(distinct content_sha256) as versions
    from {{ source('staging', 'raw_declarations') }}
    group by 1, 2, 3
), attributes as materialized (
    select tenant_id, workflow_id, run_id, versions,
        json_extract_string(document_json, '$.workflow_version') as workflow_version,
        json_extract_string(document_json, '$.experiment_version') as experiment_version,
        json_extract_string(document_json, '$.cohort_version') as cohort_version,
        json_extract_string(document_json, '$.config_version') as config_version,
        json_extract_string(document_json, '$.code_revision') as code_revision,
        json_extract_string(document_json, '$.dataset_version') as dataset_version,
        json_extract_string(document_json, '$.measurement_mode') as measurement_mode
    from declarations
)
select e.tenant_id, e.workflow_id, e.run_id, e.event_id,
    (coalesce(d.versions, 0) <> 1
        or e.workflow_version is distinct from d.workflow_version
        or e.experiment_version is distinct from d.experiment_version
        or e.cohort_version is distinct from d.cohort_version
        or e.config_version is distinct from d.config_version
        or e.code_revision is distinct from d.code_revision
        or e.dataset_version is distinct from d.dataset_version
        or e.simulated is distinct from (d.measurement_mode = 'fabricated')) as version_mismatch
from {{ ref('stg_events') }} e
left join attributes d using (tenant_id, workflow_id, run_id)
-- Comparison attestations describe a run; they never enter its evidence or rollups.
where e.event_kind <> 'comparison_attestation'
