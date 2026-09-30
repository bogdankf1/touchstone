export type Verdict = 'approve' | 'decline';
export type CaseStatus = 'all' | 'open' | 'resolved' | 'note_pending' | 'note_failed' | 'degraded';
export interface CaseSummary {
  tenant_id: string;
  case_id: string;
  decision_id: string;
  run_id: string;
  transaction_id: string;
  status: 'open' | 'resolved';
  version: number;
  created_at: string;
  note_status: string;
  degraded: boolean;
  amount_minor: string;
  currency: string;
}
export interface ReviewSummary {
  action_id: string;
  case_id: string;
  decision_id: string;
  reviewer_id: string;
  reviewer_type: 'human' | 'simulated';
  verdict: Verdict;
  recommendation: Verdict | null;
  prior_case_version: number;
  reviewed_at: string;
  idempotency_key: string;
}
export interface ReviewAction {
  tenant_id: string;
  case_id: string;
  reviewer_id: string;
  verdict: Verdict;
}
export interface GraphSnapshot {
  status: 'available' | 'unavailable';
  total_nodes: number;
  total_edges: number;
  truncated: boolean;
  cross_tenant: boolean;
  nodes: { id: string; tenant_id: string; kind: string; identity: string }[];
  edges: { id: string; source: string; target: string; kind: string }[];
}
export interface CaseNote {
  note_id: string;
  case_id: string;
  decision_id: string;
  evidence_id: string;
  prompt_version: string;
  requested_model: string;
  reported_model: string;
  generation_status: string;
  started_at: string;
  completed_at: string | null;
  verdict_recommendation: Verdict;
  confidence: { value: string | null; meaning: string };
  risk_indicators: {
    rank: number;
    indicator_id: string;
    description: string;
    method: string;
    evidence_refs: string[];
  }[];
  entity_neighbourhood: { summary: string; evidence_refs: string[] };
  comparable_cases: { transaction_id: string; summary: string; evidence_refs: string[] }[];
  what_would_change_verdict: { action: string; evidence_refs: string[] }[];
}
export interface CaseDetail extends CaseSummary {
  task_id: string;
  config_id: string;
  evidence_id: string;
  degraded_reason: string | null;
  occurred_at: string;
  raw_probability: string | null;
  effective_probability: string | null;
  effective_low_threshold: string;
  effective_high_threshold: string;
  coverage: { status: string; missing: string[] };
  cutoffs: Record<string, string | null>;
  source_snapshot_ids: Record<string, string | null>;
  graph: GraphSnapshot;
  note: CaseNote | null;
  available_before_review: boolean | null;
  review: ReviewSummary | null;
}
export interface CaseFilters {
  tenantId: string;
  runId?: string;
  status?: CaseStatus;
  cursor?: string;
}
export interface CasePage {
  items: CaseSummary[];
  next_cursor: string | null;
  limit: number;
}
export interface Thresholds {
  review_cost: string;
  margin_rate: string;
  t_low_floor: string;
  t_low_ceiling: string;
  t_high: string;
  amount_aware: boolean;
}
export interface PriceTable {
  schema_version: string;
  price_table_version: string;
  model: string;
  currency: string;
  input_per_million: string;
  output_per_million: string;
  retrieved_at: string;
  source_url: string;
}
export interface Scorer {
  provider: string;
  model: string;
  question_version: string;
  price_table: PriceTable;
}
export interface NoteModel {
  provider: string;
  model: string;
  prompt_version: string;
  price_table: PriceTable;
}
export interface RunConfiguration {
  schema_version: 'reckoner-run-config-v1';
  tenant_id: string;
  config_id: string;
  workflow_version: string;
  scorer: Scorer;
  note_model: NoteModel;
  judge_model: NoteModel;
  threshold_config_id: string;
  feature_version: string;
  scaler_id: string | null;
  calibration_id: string | null;
  score_mode: 'raw' | 'calibrated';
  graph_version: string;
  retrieval_version: string;
  resolution_policy_version: string;
  limits: {
    timeout_seconds: number;
    input_token_ceiling: number;
    max_output_tokens: number;
    maximum_attempts: number;
  };
}
export interface Qualification {
  evidence_mode: 'relational' | 'gds-augmented';
  data_kind: 'fabricated' | 'simulated-cctd';
  calibration: {
    calibration_id: string;
    fit_status: string;
    qualification: {
      status: string;
      validation_id: string;
      raw_brier: number | null;
      candidate_brier: number | null;
      raw_log_loss: number | null;
      candidate_log_loss: number | null;
    };
  } | null;
}
export interface ConfigurationEntry {
  tenant_id: string;
  config_id: string;
  configuration: RunConfiguration;
  thresholds: { parameters: Thresholds };
  qualification: Qualification | null;
}
export interface Activation {
  tenant_id: string;
  config_id: string | null;
  version: number;
}
export interface RegistryModel {
  provider: string;
  model: string;
  purpose: string;
  price_table: PriceTable;
}
export interface ConfigurationHistory {
  items: ConfigurationEntry[];
  activation: Activation;
  models: RegistryModel[];
}
export interface CreateConfiguration {
  configuration: Omit<RunConfiguration, 'config_id' | 'threshold_config_id'>;
  thresholds: Thresholds;
  evidence_mode: Qualification['evidence_mode'];
  data_kind: Qualification['data_kind'];
}
export interface ConfigurationPreview {
  tenant_id: string;
  run_id: string;
  config_id: string;
  estimate: true;
  status: 'available' | 'incompatible_scores';
  counts: Record<'auto-approve' | 'auto-decline' | 'escalate', number> | null;
  completed_decisions: number;
  missing_scores: number | null;
  model_cost: null;
  note_cost: 'not_estimated';
  provider_calls: 0;
}
