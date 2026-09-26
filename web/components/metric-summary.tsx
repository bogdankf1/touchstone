import type { Run } from '../lib/types';
import { money, percent, sumMoney } from '../lib/types';

export function MetricSummary({ run }: { run: Run }) {
  const total = sumMoney([run.model_cost, run.review_cost, run.error_cost]);
  return (
    <section aria-labelledby="summary-title">
      <div className="section-heading">
        <h2 id="summary-title">Run summary</h2>
        <span>Definition v1 · {run.measurement_mode || 'Mode unavailable'}</span>
      </div>
      <div className="metrics">
        <div>
          <span className="metric-label">Correct decisions</span>
          <strong>{percent(run.correctness)}</strong>
          <small>
            {run.correct_tasks ?? '—'} / {run.expected_tasks ?? '—'} expected tasks
          </small>
        </div>
        <div>
          <span className="metric-label">Cost per correct task</span>
          <strong>{money(run.cpst, run.currency)}</strong>
          <small>{run.cpst === null ? 'Incomplete metric' : `Exact: ${run.currency} ${run.cpst}`}</small>
        </div>
        <div>
          <span className="metric-label">Provider usage cost</span>
          <strong>{money(run.model_cost, run.currency)}</strong>
          <small>Measured calls on simulated data</small>
        </div>
        <div>
          <span className="metric-label">Total modeled cost</span>
          <strong>{money(total, run.currency)}</strong>
          <small>Provider usage + review + error assumptions</small>
        </div>
        <div>
          <span className="metric-label">p99 latency</span>
          <strong>
            {run.latency_p99_ms === null ? 'Unavailable' : `${run.latency_p99_ms.toFixed(1)} ms`}
          </strong>
          <small>{run.latency_population ?? '—'} completed root tasks</small>
        </div>
        <div>
          <span className="metric-label">Evaluation pass</span>
          <strong>{percent(run.evaluation_pass_rate)}</strong>
          <small>
            {run.passed_cases ?? '—'} / {run.expected_cases ?? '—'} cases
          </small>
        </div>
      </div>
      <div className="status-line">
        {run.metrics_complete ? 'Metrics complete' : 'Metrics incomplete'} · {run.missing_tasks ?? 'Unknown'}{' '}
        missing tasks · {run.failed_tasks ?? 'Unknown'} failed tasks · {run.missing_outcomes ?? 'Unknown'}{' '}
        outcomes missing
      </div>
      <div className="status-line muted">No compatible comparison run</div>
    </section>
  );
}
