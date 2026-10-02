import { createServer } from 'node:http';
import { reckonerRoute, resetReckoner } from './reckoner-fixtures.mjs';

let scenario = 'baseline';
const metadata = {
  generation: 'preview-001.duckdb',
  warehouse: 'DuckDB local preview',
  published_at: '2026-09-26T10:00:00Z',
  cutoff: '2026-09-26T09:59:00Z',
  accepted_measurements: 10000,
  accepted_declarations: 2,
  rejected_count: 0,
  latest_refresh: { state: 'succeeded', attempted_at: '2026-09-26T10:00:00Z', reason: null },
};
const run = (tenant_id, run_id = 'reckoner-baseline') => ({
  tenant_id,
  workflow_id: 'reckoner',
  run_id,
  workflow_version: 'v0',
  experiment_version: 'baseline',
  cohort_version: '2019',
  config_version: 'v0',
  code_revision: 'abc',
  dataset_version: 'cctd',
  price_table_version: 'v1',
  declaration_versions: 1,
  measurement_mode: 'measured',
  dataset_simulated: true,
  declared_at: '2026-09-26T08:00:00Z',
  completeness_known: true,
  metrics_complete: true,
  expected_tasks: tenant_id === 'tenant-a' ? 600 : 400,
  received_tasks: tenant_id === 'tenant-a' ? 600 : 400,
  completed_tasks: tenant_id === 'tenant-a' ? 600 : 400,
  failed_tasks: 0,
  missing_tasks: 0,
  correct_tasks: tenant_id === 'tenant-a' ? 530 : 362,
  missing_outcomes: 0,
  model_cost: tenant_id === 'tenant-a' ? '0.300000' : '0.158940',
  review_cost: tenant_id === 'tenant-a' ? '60.000000' : '36.000000',
  error_cost: tenant_id === 'tenant-a' ? '4000.000000' : '3225.337000',
  currency: 'USD',
  latency_population: tenant_id === 'tenant-a' ? 600 : 400,
  latency_p99_ms: 1315.1606670003275,
  expected_cases: tenant_id === 'tenant-a' ? 600 : 400,
  passed_cases: tenant_id === 'tenant-a' ? 530 : 362,
  missing_checks: 0,
  error_checks: 0,
  conflicting_checks: 0,
  cpst: tenant_id === 'tenant-a' ? '7.661' : '9.01',
  correctness: tenant_id === 'tenant-a' ? 530 / 600 : 362 / 400,
  completion: 1,
  evaluation_pass_rate: tenant_id === 'tenant-a' ? 530 / 600 : 362 / 400,
  metric_definitions: {
    cpst: 'v1',
    correctness: 'v1',
    completion: 'v1',
    latency_p99: 'v1',
    evaluation_pass_rate: 'v1',
  },
});
const aggregate = {
  ...run(null),
  tenant_ids: ['tenant-a', 'tenant-b'],
  excluded_tenants: [],
  tenants: [run('tenant-a'), run('tenant-b')],
  expected_tasks: 1000,
  received_tasks: 1000,
  completed_tasks: 1000,
  correct_tasks: 892,
  model_cost: '0.458940',
  review_cost: '96.000000',
  error_cost: '7225.337000',
  cpst: '8.208291412556053811659192825',
  correctness: 0.892,
  expected_cases: 1000,
  passed_cases: 892,
  evaluation_pass_rate: 0.892,
  latency_population: 1000,
};
const contributions = [
  {
    metric_id: 'missed_fraud',
    definition_version: 'v1',
    unit: 'cases',
    expected_tasks: 1000,
    task_contributions: 1000,
    incomplete_contributions: 0,
    unexpected_tasks: 0,
    missing_tasks: 0,
    numerator: 94,
    denominator: 100,
    rate: 0.94,
  },
  {
    metric_id: 'false_positive',
    definition_version: 'v1',
    unit: 'cases',
    expected_tasks: 1000,
    task_contributions: 1000,
    incomplete_contributions: 0,
    unexpected_tasks: 0,
    missing_tasks: 0,
    numerator: 14,
    denominator: 900,
    rate: 14 / 900,
  },
];
const task = {
  tenant_id: 'tenant-a',
  workflow_id: 'reckoner',
  run_id: 'reckoner-baseline',
  task_id: 'task-001',
  terminal_status: 'completed',
  latency_ms: 104.2,
  incomplete: false,
  nodes: [
    {
      node_name: 'provider_call',
      currency: 'USD',
      model_cost: '0.000459',
      call_count: 1,
      incomplete: false,
      trace_id: 'trace-001',
      evidence_event_id: 'event-001',
    },
  ],
};
const wrap = (data, route = '') => ({
  metadata: {
    ...metadata,
    generation: scenario === `generation-${route}` ? 'preview-002.duckdb' : metadata.generation,
    latest_refresh:
      scenario === 'stale'
        ? { state: 'failed', attempted_at: '2026-09-27T10:00:00Z', reason: 'latest refresh failed' }
        : metadata.latest_refresh,
  },
  data,
});
const server = createServer(async (request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1:8100');
  response.setHeader('Content-Type', 'application/json');
  if (url.pathname === '/__scenario') {
    scenario = url.searchParams.get('name') || 'baseline';
    resetReckoner();
    response.end('{}');
    return;
  }
  if (url.pathname === '/healthz') {
    response.end('{"status":"ok"}');
    return;
  }
  if (await reckonerRoute(request, response, url, scenario)) return;
  if (scenario === 'unavailable') {
    response.statusCode = 503;
    response.end('{"detail":"published snapshot unavailable"}');
    return;
  }
  if (url.pathname === '/v1/comparisons') {
    if (scenario === 'comparison-error') {
      response.statusCode = 500;
      response.end('{"detail":"comparison failed"}');
      return;
    }
    const arm = (name, cpst, evidence, mode = 'fabricated') => ({...aggregate, run_id:name, cpst,
      measurement_mode:mode, arm_provenance:[{model_version:'fixture-model',
      prompt_version:'fixture-prompt', calibration_id:'fixture-'+evidence,
      evidence_version:evidence, config_version:'fixture-config', question_version:'fixture-question',
      retrieval_window:'30/90 days', execution_mode:'fabricated', call_ids:[]} ]});
    const data = {eligible:scenario!=='comparison-ineligible',
      reasons:scenario==='comparison-ineligible'?['incompatible case membership']:[],
      delta_cpst:scenario==='comparison-ineligible'?null:'-0.5',
      baseline:arm('fixture-baseline','2.5','relational',scenario==='comparison-mixed'?'measured':'fabricated'),
      current:arm('fixture-current','2','gds-augmented')};
    response.end(JSON.stringify({metadata:{...metadata,generation:scenario==='comparison-generation'?'other':metadata.generation},data}));
    return;
  }
  if (url.pathname === '/v1/workflows') {
    response.end(
      JSON.stringify(
        wrap(
          scenario === 'empty'
            ? []
            : [
                { workflow_id: 'synthetic-ledger', tenant_count: 1, run_count: 1, tenant_ids: ['tenant-x'] },
                {
                  workflow_id: 'reckoner',
                  tenant_count: 2,
                  run_count: 3,
                  tenant_ids: ['tenant-a', 'tenant-b'],
                },
              ],
        ),
      ),
    );
    return;
  }
  if (url.pathname === '/v1/runs') {
    const tenant = url.searchParams.get('tenant_id');
    if (scenario === 'runs-error' && tenant === 'tenant-b') {
      response.statusCode = 503;
      response.end('{"detail":"published snapshot unavailable"}');
      return;
    }
    response.end(
      JSON.stringify(
        wrap(
          url.searchParams.get('workflow_id') === 'reckoner'
            ? [
                run(tenant),
                { ...run(tenant, 'reckoner-pilot'), metrics_complete: false, failed_tasks: 1, cpst: null },
              ]
            : [
                {
                  ...run(tenant, 'synthetic-run'),
                  workflow_id: 'synthetic-ledger',
                  measurement_mode: 'fabricated',
                },
              ],
          'runs',
        ),
      ),
    );
    return;
  }
  if (url.pathname.endsWith('/summary')) {
    const tenant = url.searchParams.get('tenant_id');
    const data = tenant
      ? {
          ...run(tenant),
          tenant_ids: [tenant],
          excluded_tenants: [],
          tenants: [],
          quality: { identity_conflicts: 0, incomplete_calls: 0, unavailable_prices: 0, rejected_events: 0 },
          contribution_rates: contributions,
        }
      : {
          ...aggregate,
          quality: { identity_conflicts: 0, incomplete_calls: 0, unavailable_prices: 0, rejected_events: 0 },
          contribution_rates: contributions,
        };
    if (scenario === 'offline-pending') {
      data.online_cost_complete = true;
      data.offline_cost_complete = false;
      data.offline_model_cost = null;
      data.provider_spend = null;
    }
    if (scenario === 'missing') {
      data.missing_outcomes = 5;
      data.metrics_complete = false;
      data.cpst = null;
      data.contribution_rates = contributions.map((c) => ({
        ...c,
        rate: null,
        task_contributions: 995,
        missing_tasks: 5,
      }));
    }
    if (url.pathname.includes('reckoner-pilot')) {
      data.run_id = 'reckoner-pilot';
      data.metrics_complete = false;
      data.failed_tasks = 1;
      data.cpst = null;
    }
    if (url.pathname.includes('synthetic-run')) {
      data.workflow_id = 'synthetic-ledger';
      data.run_id = 'synthetic-run';
      data.measurement_mode = 'fabricated';
      data.tenant_id = tenant;
      data.tenant_ids = ['tenant-x'];
      data.tenants = [];
      data.contribution_rates = [];
    }
    if (scenario === 'unknown-provenance') {
      data.measurement_mode = null;
      data.dataset_simulated = null;
    }
    if (scenario === 'nonsimulated-provenance') data.dataset_simulated = false;
    response.end(JSON.stringify(wrap(data, 'summary')));
    return;
  }
  if (url.pathname.endsWith('/tasks')) {
    const item = url.pathname.includes('synthetic-run')
      ? {
          ...task,
          tenant_id: 'tenant-x',
          workflow_id: 'synthetic-ledger',
          run_id: 'synthetic-run',
          task_id: 'synthetic-001',
          nodes: [],
        }
      : task;
    response.end(JSON.stringify(wrap({ items: [item], page: 1, page_size: 25, total: 1 }, 'tasks')));
    return;
  }
  if (url.pathname.startsWith('/v1/traces/')) {
    response.end(
      JSON.stringify(
        wrap(
          {
            trace_id: 'trace-001',
            events: [
              {
                tenant_id: 'tenant-a',
                workflow_id: 'reckoner',
                run_id: 'reckoner-baseline',
                task_id: 'task-001',
                event_id: 'event-001',
                event_kind: 'provider_call',
                node_name: 'provider_call',
                trace_id: 'trace-001',
                span_id: 'span-001',
                received_at: '2026-09-26T09:00:00Z',
                identity_conflict: false,
              },
            ],
            page: 1,
            page_size: 50,
            total: 1,
          },
          'trace',
        ),
      ),
    );
    return;
  }
  response.statusCode = 404;
  response.end('{}');
});
server.listen(8100, '127.0.0.1');
