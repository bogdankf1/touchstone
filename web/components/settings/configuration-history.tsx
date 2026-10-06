import type { ConfigurationHistory } from '../../lib/reckoner-types';
export function ConfigurationHistoryView({
  history,
  selected,
  onSelect,
  disabled,
}: {
  history: ConfigurationHistory;
  selected: string;
  onSelect: (id: string) => void;
  disabled: boolean;
}) {
  return (
    <section>
      <div className="section-heading">
        <h2>Immutable configuration history</h2>
        <span>{history.items.length} versions</span>
      </div>
      <div className="panel-body">
        <label className="field">
          Selected configuration
          <select value={selected} onChange={(e) => onSelect(e.target.value)} disabled={disabled}>
            {history.items.map((e) => (
              <option key={e.config_id} value={e.config_id}>
                {e.config_id}
                {e.config_id === history.activation.config_id ? ' · active' : ''}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Version / activation</th>
              <th>Thresholds</th>
              <th>Models / prompts</th>
            </tr>
          </thead>
          <tbody>
            {history.items.map((e) => (
              <tr key={e.config_id}>
                <th>
                  <button className="text-button" onClick={() => onSelect(e.config_id)} disabled={disabled}>
                    {e.config_id}
                  </button>
                  <small>
                    {e.config_id === history.activation.config_id ? 'Current defaults' : 'Saved snapshot'}
                  </small>
                </th>
                <td>
                  <span>
                    Review USD {e.thresholds.parameters.review_cost} · margin{' '}
                    {e.thresholds.parameters.margin_rate}
                  </span>
                  <small>
                    Low {e.thresholds.parameters.t_low_floor}–{e.thresholds.parameters.t_low_ceiling} · high{' '}
                    {e.thresholds.parameters.t_high} ·{' '}
                    {e.thresholds.parameters.amount_aware ? 'amount aware' : 'flat 0.05'}
                  </small>
                </td>
                <td>
                  {e.configuration.scorer.model}
                  <small>
                    {e.configuration.scorer.question_version} · {e.configuration.note_model.model} ·{' '}
                    {e.configuration.note_model.prompt_version}
                  </small>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
