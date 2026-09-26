import type { DashboardResponse, Envelope, Filters, Page, Run, Task, Trace, Workflow } from './types';

export class ApiError extends Error {
  constructor(public status: number) {
    super(`Touchstone API returned ${status}`);
  }
}

async function get<T>(path: string, params: Record<string, string> = {}): Promise<Envelope<T>> {
  const base = process.env.TOUCHSTONE_API_URL || 'http://127.0.0.1:8000';
  const url = new URL(path, base);
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new ApiError(response.status);
  return response.json() as Promise<Envelope<T>>;
}

export function getWorkflows(): Promise<Envelope<Workflow[]>> {
  return get('/v1/workflows');
}
export function getRuns(workflow: string, tenant: string): Promise<Envelope<Run[]>> {
  return get('/v1/runs', { workflow_id: workflow, tenant_id: tenant });
}
function scope(filters: Filters): Record<string, string> {
  return filters.tenant === 'all'
    ? { workflow_id: filters.workflow, aggregate: 'true' }
    : { workflow_id: filters.workflow, tenant_id: filters.tenant };
}
export function getRunSummary(filters: Filters): Promise<DashboardResponse> {
  return get(`/v1/runs/${encodeURIComponent(filters.run)}/summary`, scope(filters));
}
export function getTasks(filters: Filters): Promise<Envelope<Page<Task>>> {
  return get(`/v1/runs/${encodeURIComponent(filters.run)}/tasks`, {
    ...scope(filters),
    page: String(filters.page || 1),
    page_size: '25',
  });
}
export function getTrace(filters: Filters, trace: string, tenant: string): Promise<Envelope<Trace>> {
  return get(`/v1/traces/${encodeURIComponent(trace)}`, {
    workflow_id: filters.workflow,
    run_id: filters.run,
    tenant_id: tenant,
  });
}
