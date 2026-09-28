-- Only unambiguous execution ancestry can attribute a descendant to a declared root.
with recursive parents as (
    select tenant_id, workflow_id, run_id, task_id,
        min(parent_task_id) as parent_task_id,
        count(distinct parent_task_id)
            + cast(count(*) filter (where parent_task_id is null) > 0 as integer) as parents,
        max(cast(identity_conflict as integer)) as identity_conflict
    from {{ ref('stg_events') }}
    where event_kind = 'execution'
    group by 1, 2, 3, 4
), ancestry as (
    select t.tenant_id, t.workflow_id, t.run_id, t.task_id, t.task_id as root_task_id
    from {{ ref('int_tasks') }} t
    left join parents p using (tenant_id, workflow_id, run_id, task_id)
    where p.task_id is null or (p.parents = 1 and p.parent_task_id is null)
    union
    select p.tenant_id, p.workflow_id, p.run_id, p.task_id, a.root_task_id
    from parents p
    join ancestry a on p.tenant_id = a.tenant_id and p.workflow_id = a.workflow_id
        and p.run_id = a.run_id and p.parent_task_id = a.task_id
    where p.parents = 1 and p.identity_conflict = 0
        and not exists (
            select 1 from {{ ref('int_tasks') }} t
            where t.tenant_id = p.tenant_id and t.workflow_id = p.workflow_id
                and t.run_id = p.run_id and t.task_id = p.task_id
        )
)
select * from ancestry
