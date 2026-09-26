select tenant_id, workflow_id, run_id
from {{ ref('mart_runs') }}
group by 1, 2, 3
having count(*) > 1
