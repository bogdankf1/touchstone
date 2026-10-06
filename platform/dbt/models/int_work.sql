-- Opt-in immutable per-root work populations and separately closed cost scopes.
with declarations as (
    select tenant_id, workflow_id, run_id, task_id,
        min(document_json) as document_json,
        count(distinct cast(json_extract(document_json, '$.payload') as varchar)) as versions,
        max(cast(identity_conflict as integer)) as conflict
    from {{ ref('stg_events') }} where event_kind = 'work_declaration'
    group by 1, 2, 3, 4
), closures as (
    select tenant_id, workflow_id, run_id, task_id, cost_scope,
        min(document_json) as document_json,
        count(distinct cast(json_extract(document_json, '$.payload') as varchar)) as versions,
        max(cast(identity_conflict as integer)) as conflict
    from {{ ref('stg_events') }} where event_kind = 'work_closure'
    group by 1, 2, 3, 4, 5
), roots as (
    select t.*, s.cost_scope from {{ ref('int_tasks') }} t
    cross join (values ('online'), ('offline')) s(cost_scope)
    where exists (select 1 from {{ source('staging','raw_declarations') }} rd
        where (rd.tenant_id,rd.workflow_id,rd.run_id)=(t.tenant_id,t.workflow_id,t.run_id)
        and json_extract_string(rd.document_json,'$.lifecycle_version')='root-work-v1')
), actual as (
    select c.*, m.root_task_id from {{ ref('int_calls') }} c
    join {{ ref('int_task_membership') }} m using (tenant_id, workflow_id, run_id, task_id)
)
select t.tenant_id, t.workflow_id, t.run_id, t.task_id, t.cost_scope,
    d.document_json as declaration_json,
    (d.versions = 1 and d.conflict = 0) as declared,
    coalesce(t.terminal_status in ('completed','failed') and not t.incomplete
        and t.first_started_at is not null and t.terminal_at is not null
        and d.versions = 1 and d.conflict = 0 and c.versions = 1 and c.conflict = 0
        and json_extract_string(c.document_json, '$.payload.billing_status') = 'complete'
        and not exists (
            select 1 from json_each(d.document_json, '$.payload.evaluation_suites') ds
            where not exists (
                select 1 from {{ source('staging','raw_declarations') }} rd,
                    json_each(rd.document_json,'$.evaluation_suites') rs
                where (rd.tenant_id,rd.workflow_id,rd.run_id)=(t.tenant_id,t.workflow_id,t.run_id)
                    and json_extract_string(ds.value,'$.suite_id')=json_extract_string(rs.value,'$.suite_id')
                    and json_extract_string(ds.value,'$.suite_version')=json_extract_string(rs.value,'$.suite_version')
                    and json_extract(ds.value,'$.required_checks')=json_extract(rs.value,'$.required_checks')
            )
        )
        -- Exact identities, not just equal counts: no missing or post-closure calls.
        and not exists (
            select 1 from json_each(c.document_json, '$.payload.call_ids') ids
            where not exists (select 1 from actual a
                where (a.tenant_id,a.workflow_id,a.run_id,a.root_task_id,a.cost_scope)
                    = (t.tenant_id,t.workflow_id,t.run_id,t.task_id,t.cost_scope)
                and a.call_id = json_extract_string(ids.value, '$') and not a.incomplete)
        )
        and not exists (
            select 1 from actual a
            where (a.tenant_id,a.workflow_id,a.run_id,a.root_task_id,a.cost_scope)
                = (t.tenant_id,t.workflow_id,t.run_id,t.task_id,t.cost_scope)
            and (a.incomplete or not exists (
                select 1 from json_each(c.document_json, '$.payload.call_ids') ids
                where a.call_id = json_extract_string(ids.value, '$')))
        )
        -- Declared children must have a terminal execution and unambiguous ancestry.
        and not exists (
            select 1 from json_each(d.document_json, '$.payload.child_task_ids') child
            where not exists (
                select 1 from {{ ref('stg_events') }} e
                join {{ ref('int_task_membership') }} m
                    using (tenant_id,workflow_id,run_id,task_id)
                where (e.tenant_id,e.workflow_id,e.run_id,m.root_task_id)
                    = (t.tenant_id,t.workflow_id,t.run_id,t.task_id)
                    and e.task_id = json_extract_string(child.value, '$')
                    and e.event_kind = 'execution' and e.status in ('completed','failed')
                    and not e.identity_conflict
            )
        )
        and not exists (
            select 1 from {{ ref('int_task_membership') }} m
            where (m.tenant_id,m.workflow_id,m.run_id,m.root_task_id)
                = (t.tenant_id,t.workflow_id,t.run_id,t.task_id)
                and m.task_id <> t.task_id and not exists (
                    select 1 from json_each(d.document_json, '$.payload.child_task_ids') child
                    where m.task_id = json_extract_string(child.value, '$'))
        ), false) as complete
from roots t
left join declarations d using (tenant_id,workflow_id,run_id,task_id)
left join closures c using (tenant_id,workflow_id,run_id,task_id,cost_scope)
