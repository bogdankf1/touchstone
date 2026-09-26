select tenant_id, workflow_id, run_id, task_id, node_name, call_id
from {{ ref('int_calls') }}
group by 1, 2, 3, 4, 5, 6
having count(*) > 1
