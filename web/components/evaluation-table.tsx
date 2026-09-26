import type { Run } from '../lib/types';
import { percent } from '../lib/types';

export function EvaluationTable({ run }: { run: Run }) {
  return (
    <section aria-labelledby="evaluation-title">
      <div className="section-heading">
        <h2 id="evaluation-title">Evaluations & outcomes</h2>
        <span>Observed populations</span>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Measure</th>
              <th scope="col">Result</th>
              <th scope="col">Population</th>
              <th scope="col">Coverage</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Required checks</th>
              <td>{percent(run.evaluation_pass_rate)}</td>
              <td>
                {run.passed_cases ?? '—'} / {run.expected_cases ?? '—'}
              </td>
              <td>
                {run.missing_checks ?? '—'} missing · {run.error_checks ?? '—'} errors
              </td>
            </tr>
            {(run.contribution_rates || []).map((rate) => (
              <tr key={`${rate.metric_id}-${rate.definition_version}`}>
                <th scope="row">
                  {rate.metric_id.replaceAll('_', ' ')}{' '}
                  <span className="muted">{rate.definition_version}</span>
                </th>
                <td>{percent(rate.rate)}</td>
                <td>
                  {rate.numerator ?? '—'} / {rate.denominator ?? '—'}
                </td>
                <td>
                  {rate.task_contributions} / {rate.expected_tasks ?? '—'} tasks
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(run.contribution_rates || []).some((rate) => rate.metric_id === 'missed_fraud') && (
        <p className="callout">
          Missed fraud:{' '}
          {(() => {
            const rate = run.contribution_rates!.find((item) => item.metric_id === 'missed_fraud')!;
            return `${rate.numerator ?? '—'} / ${rate.denominator ?? '—'}`;
          })()}{' '}
          observed cases. Correctness alone does not describe this error rate.
        </p>
      )}
    </section>
  );
}
