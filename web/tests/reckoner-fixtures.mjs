import { readFileSync } from 'node:fs';
// All operational fixtures are fabricated, simulated evidence, without oracle fields.
const price = (name) =>
  JSON.parse(
    readFileSync(new URL(`../../workloads/reckoner/config/${name}-prices-v1.json`, import.meta.url)),
  );
const model = 'anthropic/claude-haiku-4-5-20251001';
const entry = (tenant, id = 'config-a') => ({
  tenant_id: tenant,
  config_id: id,
  configuration: {
    schema_version: 'reckoner-run-config-v1',
    tenant_id: tenant,
    config_id: id,
    workflow_version: 'reckoner-v1',
    scorer: {
      provider: 'typesafe',
      model: 'jev-1.13.0',
      question_version: 'binary-v1',
      price_table: price('jev'),
    },
    note_model: { provider: 'anthropic', model, prompt_version: 'note-v1', price_table: price('anthropic') },
    judge_model: {
      provider: 'anthropic',
      model,
      prompt_version: 'judge-v1',
      price_table: price('anthropic'),
    },
    threshold_config_id: 'threshold-a',
    feature_version: 'features-v1',
    scaler_id: null,
    calibration_id: 'calibration-a',
    score_mode: 'calibrated',
    graph_version: 'graph-v1',
    retrieval_version: 'retrieval-v1',
    resolution_policy_version: 'simulated-seven-days-v1',
    limits: { timeout_seconds: 30, input_token_ceiling: 2000, max_output_tokens: 1000, maximum_attempts: 1 },
  },
  thresholds: {
    parameters: {
      review_cost: '4.00',
      margin_rate: '0.30',
      t_low_floor: '0.005',
      t_low_ceiling: '0.05',
      t_high: '0.90',
      amount_aware: true,
    },
  },
  qualification: {
    evidence_mode: 'relational',
    data_kind: 'fabricated',
    calibration: {
      calibration_id: 'calibration-a',
      fit_status: 'succeeded',
      qualification: {
        status: 'selected',
        validation_id: 'validation-a',
        raw_brier: 0.2,
        candidate_brier: 0.1,
        raw_log_loss: 0.3,
        candidate_log_loss: 0.2,
      },
    },
  },
});
let cases, histories, receipts, reviews, writes;
export function resetReckoner() {
  cases = {};
  histories = {};
  receipts = {};
  reviews = {};
  writes = [];
}
resetReckoner();
function detail(tenant, id, s) {
  const c = {
    tenant_id: tenant,
    case_id: id,
    decision_id: 'decision-a',
    run_id: 'run-a',
    transaction_id: `txn-${id}`,
    status: 'open',
    version: 1,
    created_at: '2019-01-08T00:00:00Z',
    occurred_at: '2019-01-08T00:00:00Z',
    note_status: 'succeeded',
    degraded: false,
    amount_minor: '40000',
    currency: 'USD',
    task_id: 'task-a',
    config_id: 'config-a',
    evidence_id: 'evidence-a',
    degraded_reason: null,
    raw_probability: '0.2',
    effective_probability: '0.2',
    effective_low_threshold: '0.01',
    effective_high_threshold: '0.90',
    coverage: { status: 'available', missing: [] },
    cutoffs: { history_before: '2019-01-08T00:00:00Z', graph_before: '2019-01-07T00:00:00Z' },
    source_snapshot_ids: { postgres: 'snapshot-a', graph: 'graph-a' },
    graph: {
      status: 'available',
      total_nodes: 120,
      total_edges: 240,
      truncated: true,
      cross_tenant: true,
      nodes: [
        { id: 'node-a', tenant_id: tenant, kind: 'card', identity: 'Card 42' },
        { id: 'node-b', tenant_id: 'tenant-b', kind: 'device', identity: 'Shared device' },
      ],
      edges: [{ id: 'edge-a', source: 'node-a', target: 'node-b', kind: 'uses' }],
    },
    note: {
      note_id: 'note-a',
      case_id: id,
      decision_id: 'decision-a',
      evidence_id: 'evidence-a',
      prompt_version: 'note-v1',
      requested_model: model,
      reported_model: model,
      generation_status: 'succeeded',
      started_at: '2019-01-08T00:00:00Z',
      completed_at: '2019-01-08T00:00:01Z',
      verdict_recommendation: 'approve',
      confidence: { value: '0.8', meaning: 'raw_choice_probability' },
      risk_indicators: [
        {
          rank: 1,
          indicator_id: 'indicator-a',
          description: '<img src=x onerror=alert(1)>',
          method: 'velocity',
          evidence_refs: ['evidence-a'],
        },
      ],
      entity_neighbourhood: { summary: 'Device shared across tenants.', evidence_refs: ['graph-a'] },
      comparable_cases: [],
      what_would_change_verdict: [{ action: 'Confirm card possession', evidence_refs: ['snapshot-a'] }],
    },
    available_before_review: true,
    review: null,
  };
  if (s === 'large-amount') c.amount_minor = '9007199254740993';
  if (s === 'crowded-graph') {
    c.graph.nodes = Array.from({ length: 100 }, (_, i) => ({
      id: `node-${i}`,
      tenant_id: tenant,
      kind: 'device',
      identity: `Device ${i}`,
    }));
    c.graph.edges = Array.from({ length: 200 }, (_, i) => ({
      id: `edge-${i}`,
      source: `node-${i % 100}`,
      target: `node-${(i + 1) % 100}`,
      kind: 'shares',
    }));
  }
  if (s === 'note-failed' || s === 'note-pending') {
    c.note = null;
    c.note_status = s === 'note-failed' ? 'failed' : 'pending';
  }
  if (s === 'degraded') {
    c.degraded = true;
    c.degraded_reason = 'scorer unavailable';
    c.effective_probability = null;
    c.note = null;
  }
  if (s === 'no-graph')
    c.graph = { ...c.graph, status: 'unavailable', nodes: [], edges: [], truncated: false };
  if (s === 'empty-graph')
    c.graph = { ...c.graph, total_nodes: 0, total_edges: 0, nodes: [], edges: [], truncated: false };
  if (s === 'resolved' || s === 'late-note') {
    c.status = 'resolved';
    c.version = 2;
    c.review = {
      action_id: 'action-old',
      case_id: id,
      decision_id: 'decision-a',
      reviewer_id: 'alex',
      reviewer_type: 'human',
      verdict: 'decline',
      recommendation: null,
      prior_case_version: 1,
      reviewed_at: '2019-01-08T00:00:01Z',
      idempotency_key: 'old',
    };
    c.available_before_review = false;
  }
  return c;
}
async function body(r) {
  let raw = '';
  for await (const chunk of r) raw += chunk;
  return JSON.parse(raw);
}
export async function reckonerRoute(request, response, url, s) {
  const send = (d, status = 200) => {
    response.statusCode = status;
    response.end(JSON.stringify(d));
  };
  const tenant = url.searchParams.get('tenant_id') || 'tenant-a';
  if (url.pathname === '/__writes') {
    send(writes);
    return true;
  }
  if (
    ![
      '/v1/cases',
      '/v1/case',
      '/v1/reviews',
      '/v1/configurations',
      '/v1/configurations/activate',
      '/v1/configurations/preview',
    ].includes(url.pathname)
  )
    return false;
  if (s === 'reckoner-unavailable') {
    send({ detail: 'unavailable' }, 503);
    return true;
  }
  const history = (t) =>
    (histories[t] ||= {
      items: [entry(t)],
      activation: { tenant_id: t, config_id: 'config-a', version: 1 },
      models: [
        { provider: 'typesafe', model: 'jev-1.13.0', purpose: 'scorer', price_table: price('jev') },
        { provider: 'anthropic', model, purpose: 'note', price_table: price('anthropic') },
        { provider: 'fake', model: 'unsupported/fake', purpose: 'scorer', price_table: {} },
      ],
    });
  if (url.pathname === '/v1/cases') {
    let items =
      s === 'review-empty'
        ? []
        : Array.from({ length: s === 'crowded' ? 100 : 2 }, (_, i) => {
            const id = `case-${String(i + 1).padStart(3, '0')}`;
            return (cases[`${tenant}/${id}`] ||= detail(tenant, id, s));
          });
    const status = url.searchParams.get('status');
    if (status && status !== 'all')
      items = items.filter((c) =>
        status === 'open' || status === 'resolved'
          ? c.status === status
          : status === 'degraded'
            ? c.degraded
            : status === 'note_pending'
              ? c.note_status === 'pending'
              : c.note_status === 'failed',
      );
    const offset = Number(url.searchParams.get('cursor') || 0);
    send({
      items: items.slice(offset, offset + 50),
      next_cursor: items.length > offset + 50 ? String(offset + 50) : null,
      limit: 50,
    });
  } else if (url.pathname === '/v1/case') {
    const id = url.searchParams.get('case_id');
    const c = (cases[`${tenant}/${id}`] ||= detail(tenant, id, s));
    if (s === 'review-read-lost' && c.status === 'resolved' && !c.refresh_failed) {
      c.refresh_failed = true;
      send({}, 503);
    } else send(c);
  } else if (url.pathname === '/v1/reviews') {
    const b = await body(request);
    writes.push(b);
    const c = cases[`${b.tenant_id}/${b.case_id}`];
    if (reviews[b.idempotency_key]) send(reviews[b.idempotency_key]);
    else if (s === 'review-stale' || c.status === 'resolved' || b.expected_version !== c.version)
      send({ detail: 'stale' }, 409);
    else if (!b.reviewer_id || /^simulat/i.test(b.reviewer_id)) send({}, 422);
    else {
      const r = {
        ...b,
        action_id: 'action-new',
        reviewer_type: 'human',
        recommendation: c.note?.verdict_recommendation || null,
        prior_case_version: b.expected_version,
        reviewed_at: '2019-01-08T00:00:02Z',
      };
      reviews[b.idempotency_key] = r;
      c.status = 'resolved';
      c.version++;
      c.review = r;
      if (s === 'review-lost') send({}, 503);
      else send(r);
    }
  } else if (url.pathname === '/v1/configurations' && request.method === 'GET')
    send(
      s === 'settings-empty'
        ? { ...history(tenant), items: [], activation: { tenant_id: tenant, config_id: null, version: 0 } }
        : history(tenant),
    );
  else if (url.pathname === '/v1/configurations') {
    const b = await body(request);
    writes.push(b);
    if (b.configuration.scorer.question_version !== 'binary-v1' && b.configuration.calibration_id)
      send({ detail: 'context mismatch' }, 422);
    else {
      const h = history(b.configuration.tenant_id);
      const id = `config-${h.items.length + 1}`;
      const e = {
        ...entry(b.configuration.tenant_id, id),
        configuration: { ...b.configuration, config_id: id, threshold_config_id: 'threshold-new' },
        thresholds: { parameters: b.thresholds },
      };
      h.items.push(e);
      send(e);
    }
  } else if (url.pathname === '/v1/configurations/activate') {
    const b = await body(request);
    writes.push(b);
    const h = history(b.tenant_id);
    if (receipts[b.idempotency_key]) send(receipts[b.idempotency_key]);
    else if (s === 'activation-stale' || b.expected_version !== h.activation.version) {
      h.activation.version = 3;
      send({}, 409);
    } else {
      h.activation = { tenant_id: b.tenant_id, config_id: b.config_id, version: h.activation.version + 1 };
      receipts[b.idempotency_key] = { ...h.activation };
      if (s === 'activation-lost') {
        h.activation = { ...h.activation, version: 4, config_id: 'config-a' };
        send({}, 503);
      } else send(h.activation);
    }
  } else
    send({
      tenant_id: tenant,
      run_id: url.searchParams.get('run_id'),
      config_id: url.searchParams.get('config_id'),
      estimate: true,
      status: s === 'preview-incompatible' ? 'incompatible_scores' : 'available',
      counts: s === 'preview-incompatible' ? null : { 'auto-approve': 1, 'auto-decline': 1, escalate: 3 },
      completed_decisions: 5,
      missing_scores: 1,
      model_cost: null,
      note_cost: 'not_estimated',
      provider_calls: 0,
    });
  return true;
}
