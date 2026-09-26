import { ApiError, getRunSummary, getRuns, getTasks, getTrace, getWorkflows } from '../lib/api';
import type { Filters, Run } from '../lib/types';
import { RunFilters } from '../components/run-filters';
import { MetricSummary } from '../components/metric-summary';
import { CostTable } from '../components/cost-table';
import { EvaluationTable } from '../components/evaluation-table';
import { TaskTable } from '../components/task-table';
import { RefreshState } from '../components/refresh-state';
import Link from 'next/link';

type Params = Record<string, string | string[] | undefined>;
function param(values: Params, name: string) {
  const value = values[name];
  return typeof value === 'string' ? value : '';
}
function chooseRun(runs: Run[]): Run | undefined {
  return [...runs].sort((a, b) => {
    const score = (item: Run) =>
      (item.measurement_mode === 'measured' ? 4 : 0) +
      (item.metrics_complete ? 2 : 0) +
      ((item.experiment_version || item.run_id).toLowerCase().includes('baseline') ? 1 : 0);
    return score(b) - score(a) || a.run_id.localeCompare(b.run_id);
  })[0];
}

export default async function Home({ searchParams }: { searchParams: Promise<Params> }) {
  const params = await searchParams;
  let discovery;
  try {
    discovery = await getWorkflows();
  } catch (error) {
    return (
      <Shell>
        <div className="empty">
          <h2>No published snapshot available</h2>
          <p>
            {error instanceof ApiError && error.status === 503
              ? 'The local read API has no available published generation.'
              : 'The local read API is unavailable.'}
          </p>
        </div>
      </Shell>
    );
  }
  const workflows = discovery.data;
  if (!workflows.length)
    return (
      <Shell>
        <RefreshState metadata={discovery.metadata} />
        <div className="empty">
          <h2>No workflows in this snapshot</h2>
          <p>Publish a run declaration and refresh the warehouse to populate this view.</p>
        </div>
      </Shell>
    );
  const allRuns = (
    await Promise.all(
      workflows.flatMap((workflow) =>
        workflow.tenant_ids.map(async (tenant) => {
          try {
            return (await getRuns(workflow.workflow_id, tenant)).data;
          } catch {
            return [];
          }
        }),
      ),
    )
  ).flat();
  const wantedWorkflow = param(params, 'workflow');
  const defaultRun = chooseRun(allRuns);
  const workflow =
    workflows.find((item) => item.workflow_id === wantedWorkflow) ||
    workflows.find((item) => item.workflow_id === defaultRun?.workflow_id) ||
    workflows[0];
  const wantedTenant = param(params, 'tenant');
  const tenant = workflow.tenant_ids.includes(wantedTenant) ? wantedTenant : 'all';
  const workflowRuns = allRuns.filter(
    (run) => run.workflow_id === workflow.workflow_id && (tenant === 'all' || run.tenant_id === tenant),
  );
  const wantedRun = param(params, 'run');
  const selectedRun = workflowRuns.find((run) => run.run_id === wantedRun) || chooseRun(workflowRuns);
  const filters: Filters = {
    workflow: workflow.workflow_id,
    tenant,
    run: selectedRun?.run_id || '',
    page: Math.max(1, Number.parseInt(param(params, 'page'), 10) || 1),
  };
  return (
    <Shell>
      <RefreshState metadata={discovery.metadata} />
      <RunFilters workflows={workflows} runs={allRuns} selected={filters} />
      {selectedRun ? (
        <RunContent
          filters={filters}
          traceId={param(params, 'trace')}
          traceTenant={param(params, 'trace_tenant')}
        />
      ) : (
        <div className="empty">
          <h2>No runs for this selection</h2>
          <p>This workflow has no selectable run in the published snapshot.</p>
        </div>
      )}
    </Shell>
  );
}

async function RunContent({
  filters,
  traceId,
  traceTenant,
}: {
  filters: Filters;
  traceId: string;
  traceTenant: string;
}) {
  let summary;
  let tasks;
  let trace = null;
  try {
    [summary, tasks] = await Promise.all([getRunSummary(filters), getTasks(filters)]);
    if (
      traceId &&
      traceTenant &&
      (filters.tenant === 'all' || filters.tenant === traceTenant) &&
      summary.data.tenant_ids?.includes(traceTenant)
    ) {
      try {
        trace = (await getTrace(filters, traceId, traceTenant)).data;
      } catch {
        trace = null;
      }
    }
  } catch {
    return (
      <div className="empty">
        <h2>Run details unavailable</h2>
        <p>The selected run could not be read from this snapshot.</p>
      </div>
    );
  }
  const run = summary.data;
  return (
    <>
      <div className="run-heading">
        <div>
          <span className="eyebrow">WORKFLOW / RUN</span>
          <h1>
            {run.workflow_id} <span className="slash">/</span> {run.run_id}
          </h1>
          <p>
            {filters.tenant === 'all' ? `${run.tenant_ids?.length ?? 0} compatible tenants` : filters.tenant}{' '}
            · {run.config_version || 'Config unknown'} · {run.cohort_version || 'Cohort unknown'}
          </p>
        </div>
        <div className="badges">
          <span>Simulated source data</span>
          <span>
            {run.measurement_mode === 'fabricated' ? 'Fabricated measurements' : 'Measured provider calls'}
          </span>
          <span>Local preview</span>
        </div>
      </div>
      {(run.excluded_tenants?.length || 0) > 0 && (
        <p className="callout">Excluded incompatible tenants: {run.excluded_tenants!.join(', ')}</p>
      )}
      <MetricSummary run={run} />
      <div className="content-grid">
        <CostTable run={run} tasks={tasks.data.items} />
        <EvaluationTable run={run} />
      </div>
      <TaskTable tasks={tasks.data} filters={filters} trace={trace} />
    </>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">T</span>
          <span>TOUCHSTONE</span>
        </div>
        <nav aria-label="Primary">
          <Link className="active" href="/">
            Runs
          </Link>
          <span>Evaluations</span>
          <span>Data health</span>
        </nav>
        <div className="sidebar-foot">
          LOCAL PREVIEW
          <br />
          Simulated source data
        </div>
      </aside>
      <main id="main">
        <header className="topbar">
          <span>Measurement / Runs</span>
          <span>Touchstone v1</span>
        </header>
        <div className="main-inner">{children}</div>
      </main>
    </div>
  );
}
