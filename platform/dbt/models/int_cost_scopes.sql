-- Price snapshots are compatible per pinned provider/model, never globally.
with prices as (
    select tenant_id, workflow_id, run_id, provider, model,
        count(distinct price_table_version) > 1 as conflict
    from {{ ref('int_calls') }} group by 1,2,3,4,5
), costs as (
    select c.tenant_id,c.workflow_id,c.run_id,c.cost_scope,
        sum(c.cost_amount) as cost, count(*) as call_count,
        count(distinct c.currency) as currencies, min(c.currency) as currency,
        bool_or(c.incomplete or p.conflict or m.task_id is null or exists (
            select 1 from {{ source('staging','raw_declarations') }} d
            where (d.tenant_id,d.workflow_id,d.run_id)=(c.tenant_id,c.workflow_id,c.run_id)
            and json_extract(d.document_json,'$.provider_prices') is not null
            and not exists (select 1 from json_each(d.document_json,'$.provider_prices') pin
                where json_extract_string(pin.value,'$.provider')=c.provider
                and json_extract_string(pin.value,'$.model')=c.model
                and json_extract_string(pin.value,'$.price_table_version')=c.price_table_version)
        )) as incomplete
    from {{ ref('int_calls') }} c
    join prices p using (tenant_id,workflow_id,run_id,provider,model)
    left join {{ ref('int_task_membership') }} m using (tenant_id,workflow_id,run_id,task_id)
    group by 1,2,3,4
), roots as (
    select tenant_id,workflow_id,run_id,cost_scope,bool_and(complete) as complete
    from {{ ref('int_work') }} group by 1,2,3,4
), runs as (
    select tenant_id,workflow_id,run_id,
        bool_or(json_extract_string(document_json,'$.lifecycle_version') = 'root-work-v1') as lifecycle
    from {{ source('staging','raw_declarations') }} group by 1,2,3
)
select r.tenant_id,r.workflow_id,r.run_id,s.cost_scope,
    coalesce(c.cost,cast(0 as decimal(38,12))) as observed_cost,
    c.currency, coalesce(c.call_count,0) as call_count,
    (not coalesce(c.incomplete,false) and coalesce(c.currencies,0) <= 1
        and (not coalesce(r.lifecycle,false) or coalesce(w.complete,false))) as complete
from runs r cross join (values ('online'),('offline')) s(cost_scope)
left join costs c using (tenant_id,workflow_id,run_id,cost_scope)
left join roots w using (tenant_id,workflow_id,run_id,cost_scope)
