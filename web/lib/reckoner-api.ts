import type {
  Activation,
  CaseDetail,
  CaseFilters,
  CasePage,
  ConfigurationEntry,
  ConfigurationHistory,
  ConfigurationPreview,
  CreateConfiguration,
  ReviewAction,
  ReviewSummary,
} from './reckoner-types';
export class ReckonerApiError extends Error {
  constructor(public status: number) {
    super(`Reckoner request failed (${status})`);
  }
}
async function request<T>(path: string, params: Record<string, string> = {}, body?: unknown): Promise<T> {
  const query = new URLSearchParams(params);
  const response = await fetch(`/api/reckoner/${path}?${query}`, {
    method: body ? 'POST' : 'GET',
    cache: 'no-store',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new ReckonerApiError(response.status);
  return response.json() as Promise<T>;
}
export function listCases(filters: CaseFilters) {
  return request<CasePage>('cases', {
    tenant_id: filters.tenantId,
    status: filters.status || 'all',
    limit: '50',
    ...(filters.runId ? { run_id: filters.runId } : {}),
    ...(filters.cursor ? { cursor: filters.cursor } : {}),
  });
}
export function getCase(tenantId: string, caseId: string) {
  return request<CaseDetail>('case', { tenant_id: tenantId, case_id: caseId });
}
export function submitReview(action: ReviewAction, idempotencyKey: string, expectedVersion: number) {
  return request<ReviewSummary>(
    'reviews',
    {},
    { ...action, idempotency_key: idempotencyKey, expected_version: expectedVersion },
  );
}
export function listConfigurations(tenantId: string) {
  return request<ConfigurationHistory>('configurations', { tenant_id: tenantId });
}
export function createConfiguration(document: CreateConfiguration) {
  return request<ConfigurationEntry>('configurations', {}, document);
}
export function activateConfiguration(
  tenantId: string,
  configId: string,
  expectedVersion: number,
  idempotencyKey: string,
) {
  return request<Activation>(
    'configurations/activate',
    {},
    {
      tenant_id: tenantId,
      config_id: configId,
      expected_version: expectedVersion,
      idempotency_key: idempotencyKey,
    },
  );
}
export function previewConfiguration(tenantId: string, runId: string, configId: string) {
  return request<ConfigurationPreview>('configurations/preview', {
    tenant_id: tenantId,
    run_id: runId,
    config_id: configId,
  });
}
export function supportedModel(model: {
  provider: string;
  model: string;
  purpose: string;
  price_table: {
    model?: string;
    price_table_version?: string;
    input_per_million?: string;
    output_per_million?: string;
    currency?: string;
  };
}) {
  return (
    ((model.purpose === 'scorer' && model.provider === 'typesafe' && model.model === 'jev-1.13.0') ||
      (model.purpose === 'note' &&
        model.provider === 'anthropic' &&
        model.model === 'anthropic/claude-haiku-4-5-20251001')) &&
    model.price_table?.model === model.model &&
    model.price_table.currency === 'USD' &&
    !!model.price_table.price_table_version &&
    /^\d+(\.\d+)?$/.test(model.price_table.input_per_million || '') &&
    /^\d+(\.\d+)?$/.test(model.price_table.output_per_million || '')
  );
}
