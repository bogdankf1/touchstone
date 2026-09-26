'use client';

import { useState } from 'react';
import type { Filters, Run, Workflow } from '../lib/types';

export function RunFilters({
  workflows,
  runs,
  selected,
}: {
  workflows: Workflow[];
  runs: Run[];
  selected: Filters;
}) {
  const [workflow, setWorkflow] = useState(selected.workflow);
  const [tenant, setTenant] = useState(selected.tenant);
  const [run, setRun] = useState(selected.run);
  const workflowItem = workflows.find((item) => item.workflow_id === workflow);
  const options = [
    ...new Map(
      runs
        .filter((item) => item.workflow_id === workflow && (tenant === 'all' || item.tenant_id === tenant))
        .map((item) => [item.run_id, item]),
    ).values(),
  ];
  const selectedRun = options.some((item) => item.run_id === run) ? run : options[0]?.run_id || '';
  return (
    <form className="filters" action="/" method="get">
      <label>
        Workflow
        <select
          name="workflow"
          value={workflow}
          onChange={(event) => {
            setWorkflow(event.target.value);
            setTenant('all');
            setRun('');
          }}
        >
          {workflows.map((item) => (
            <option key={item.workflow_id} value={item.workflow_id}>
              {item.workflow_id}
            </option>
          ))}
        </select>
      </label>
      <label>
        Run
        <select name="run" value={selectedRun} onChange={(event) => setRun(event.target.value)}>
          {options.map((item) => (
            <option key={item.run_id} value={item.run_id}>
              {item.run_id}
            </option>
          ))}
        </select>
      </label>
      <label>
        Tenant
        <select
          name="tenant"
          value={tenant}
          onChange={(event) => {
            setTenant(event.target.value);
            setRun('');
          }}
        >
          <option value="all">All compatible tenants</option>
          {workflowItem?.tenant_ids.map((id) => (
            <option key={id} value={id}>
              {id}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" disabled={!selectedRun}>
        Apply filters
      </button>
    </form>
  );
}
