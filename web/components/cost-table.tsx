import type { Run, Task } from '../lib/types';
import { money } from '../lib/types';

export function CostTable({ run, tasks }: { run: Run; tasks: Task[] }) {
  return (
    <section aria-labelledby="cost-title">
      <div className="section-heading">
        <h2 id="cost-title">Cost attribution</h2>
        <span>Exact warehouse decimal values</span>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Component</th>
              <th scope="col">Basis</th>
              <th scope="col" className="number">
                Amount
              </th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">
                {run.measurement_mode === 'measured'
                  ? 'Provider calls'
                  : run.measurement_mode === 'fabricated'
                    ? 'Fabricated call costs'
                    : 'Provider cost (mode unknown)'}
              </th>
              <td>
                {run.measurement_mode === 'measured'
                  ? 'Usage priced'
                  : run.measurement_mode === 'fabricated'
                    ? 'Fabricated amount'
                    : 'Basis unavailable'}
              </td>
              <td className="number">{money(run.model_cost, run.currency)}</td>
            </tr>
            {run.provider_spend !== undefined && (
              <>
                <tr>
                  <th scope="row">Offline model cost</th>
                  <td>Excluded from online cost per task</td>
                  <td className="number">{money(run.offline_model_cost ?? null, run.currency)}</td>
                </tr>
                <tr>
                  <th scope="row">Total provider spend</th>
                  <td>Online and offline calls</td>
                  <td className="number">{money(run.provider_spend, run.currency)}</td>
                </tr>
              </>
            )}
            <tr>
              <th scope="row">Review</th>
              <td>Modeled assumption</td>
              <td className="number">{money(run.review_cost, run.currency)}</td>
            </tr>
            <tr>
              <th scope="row">Errors</th>
              <td>Modeled assumption</td>
              <td className="number">{money(run.error_cost, run.currency)}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <h3>Node costs in visible tasks</h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Node</th>
              <th scope="col">Task</th>
              <th scope="col">Calls</th>
              <th scope="col" className="number">
                {run.provider_spend !== undefined ? 'Attributed online cost' : 'Attributed cost'}
              </th>
              {run.provider_spend !== undefined && (
                <th scope="col" className="number">
                  Node provider spend
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {tasks.flatMap((task) =>
              task.nodes.map((node, index) => (
                <tr key={`${task.tenant_id}-${task.task_id}-${index}`}>
                  <th scope="row">{node.node_name}</th>
                  <td>{task.task_id}</td>
                  <td>{node.call_count}</td>
                  <td className="number">{money(node.model_cost, node.currency)}</td>
                  {run.provider_spend !== undefined && (
                    <td className="number">{money(node.provider_spend ?? null, node.currency)}</td>
                  )}
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
