import type { Comparison, Filters, Run } from '../lib/types';
import { money } from '../lib/types';

export function RunComparison({ comparison, changed, failed, filters, runs, selected }: {
  comparison: Comparison | null; changed: boolean; failed: boolean; filters: Filters; runs: Run[]; selected: string;
}) {
  const choices = [...new Set(runs.filter(r => r.run_id !== filters.run).map(r => r.run_id))];
  return <section aria-labelledby="comparison-title">
    <h2 id="comparison-title">Baseline / current comparison</h2>
    <form method="get">
      <input type="hidden" name="workflow" value={filters.workflow}/>
      <input type="hidden" name="tenant" value={filters.tenant}/>
      <input type="hidden" name="run" value={filters.run}/>
      <label>Compare with <select name="compare" defaultValue={selected}>
        <option value="">Select current run</option>
        {choices.map(id => <option key={id} value={id}>{id}</option>)}
      </select></label> <button type="submit">Compare runs</button>
    </form>
    {failed ? <p role="alert">Comparison unavailable: the comparison could not be read from this snapshot.</p>
      : changed ? <p>Comparison snapshot changed</p> : !comparison ? <p>No compatible comparison run</p> : <>
      {[comparison.baseline, comparison.current].some(arm => arm.measurement_mode === 'fabricated')
        ? <p>Simulated comparison fixture</p>
        : [comparison.baseline, comparison.current].some(arm => arm.dataset_simulated !== false)
          && <p>Simulated dataset comparison</p>}
      <table><thead><tr><th>Arm</th><th>Run</th><th>CPST</th><th>Evidence provenance</th></tr></thead>
        <tbody>{(['baseline','current'] as const).map(key => <tr key={key}>
          <th>{key}</th><td>{comparison[key].run_id}</td>
          <td>{money(comparison[key].cpst, comparison[key].currency)}</td>
          <td><details><summary>Arm versions and call identities</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(comparison[key].arm_provenance,null,2)}</pre></details></td>
        </tr>)}</tbody></table>
      {comparison.eligible && comparison.delta_cpst !== null
        ? <p>CPST change: {money(comparison.delta_cpst, comparison.current.currency)}</p>
        : <><p>Comparison ineligible</p><p>{comparison.reasons.join('; ')}</p></>}
    </>}
  </section>;
}
