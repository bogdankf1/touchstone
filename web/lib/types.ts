export type Metadata = {
  generation: string;
  warehouse: string;
  published_at: string | null;
  cutoff: string | null;
  accepted_measurements?: number | null;
  accepted_declarations?: number | null;
  rejected_count?: number | null;
  latest_refresh: {
    state: 'unknown' | 'succeeded' | 'failed' | 'published_with_warning';
    attempted_at: string | null;
    reason: string | null;
  };
};
export type Envelope<T> = { metadata: Metadata; data: T };
export type Workflow = { workflow_id: string; tenant_count: number; run_count: number; tenant_ids: string[] };
export type Run = {
  tenant_id: string | null;
  tenant_ids?: string[];
  excluded_tenants?: string[];
  tenants?: Run[];
  workflow_id: string;
  run_id: string;
  workflow_version: string | null;
  experiment_version: string | null;
  cohort_version: string | null;
  config_version: string | null;
  measurement_mode: string | null;
  dataset_simulated: boolean | null;
  completeness_known: boolean;
  metrics_complete: boolean;
  expected_tasks: number | null;
  received_tasks: number | null;
  completed_tasks: number | null;
  failed_tasks: number | null;
  missing_tasks: number | null;
  correct_tasks: number | null;
  missing_outcomes: number | null;
  model_cost: string | null;
  review_cost: string | null;
  error_cost: string | null;
  cpst: string | null;
  currency: string | null;
  correctness: number | null;
  completion: number | null;
  latency_population: number | null;
  latency_p99_ms: number | null;
  expected_cases: number | null;
  passed_cases: number | null;
  missing_checks: number | null;
  error_checks: number | null;
  conflicting_checks: number | null;
  evaluation_pass_rate: number | null;
  metric_definitions: Record<string, string>;
  code_revision?: string | null;
  dataset_version?: string | null;
  price_table_version?: string | null;
  quality?: {
    identity_conflicts: number;
    incomplete_calls: number;
    unavailable_prices: number;
    rejected_events: number;
  };
  contribution_rates?: ContributionRate[];
};
export type ContributionRate = {
  metric_id: string;
  definition_version: string;
  unit: string;
  expected_tasks: number | null;
  task_contributions: number;
  incomplete_contributions: number;
  unexpected_tasks: number;
  missing_tasks: number | null;
  numerator: number | null;
  denominator: number | null;
  rate: number | null;
};
export type DashboardResponse = Envelope<Run>;
export type NodeCost = {
  node_name: string;
  currency: string | null;
  model_cost: string | null;
  call_count: number;
  incomplete: boolean;
  trace_id: string | null;
  evidence_event_id: string | null;
};
export type Task = {
  tenant_id: string;
  workflow_id: string;
  run_id: string;
  task_id: string;
  terminal_status: string | null;
  latency_ms: number | null;
  incomplete: boolean;
  nodes: NodeCost[];
};
export type Page<T> = { items: T[]; page: number; page_size: number; total: number };
export type TraceEvent = {
  tenant_id: string;
  workflow_id: string;
  run_id: string;
  task_id: string;
  event_id: string;
  event_kind: string;
  node_name: string;
  trace_id: string;
  span_id: string;
  received_at: string;
  identity_conflict: boolean;
};
export type Trace = {
  trace_id: string;
  events: TraceEvent[];
  page: number;
  page_size: number;
  total: number;
};
export type Filters = { workflow: string; run: string; tenant: string; page?: number };

export function money(value: string | null, currency: string | null, places = 6): string {
  if (value === null || currency === null) return 'Unavailable';
  const [whole, fraction = ''] = value.split('.');
  const sign = whole.startsWith('-') ? '-' : '';
  const digits = whole.replace('-', '').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return `${currency} ${sign}${digits}.${fraction.padEnd(places, '0').slice(0, places)}`;
}

export function sumMoney(values: (string | null)[]): string | null {
  if (values.some((value) => value === null)) return null;
  const scale = 12;
  const sum = values.reduce<bigint>((total, value) => {
    const [whole, fraction = ''] = value!.split('.');
    const sign = whole.startsWith('-') ? -1n : 1n;
    return (
      total +
      sign *
        (BigInt(whole.replace('-', '')) * 10n ** BigInt(scale) +
          BigInt(fraction.padEnd(scale, '0').slice(0, scale)))
    );
  }, 0n);
  const negative = sum < 0n;
  const absolute = negative ? -sum : sum;
  return `${negative ? '-' : ''}${absolute / 10n ** BigInt(scale)}.${String(absolute % 10n ** BigInt(scale)).padStart(scale, '0')}`;
}

export function percent(value: number | null): string {
  return value === null ? 'Unavailable' : `${(value * 100).toFixed(1)}%`;
}
