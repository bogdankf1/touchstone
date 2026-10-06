'use client';
import { useRef, useState } from 'react';
import { createConfiguration, ReckonerApiError, supportedModel } from '../../lib/reckoner-api';
import type { ConfigurationEntry, RegistryModel, Thresholds, Qualification } from '../../lib/reckoner-types';
const fields: [keyof Omit<Thresholds, 'amount_aware'>, string][] = [
  ['review_cost', 'Review cost (USD)'],
  ['margin_rate', 'Margin rate'],
  ['t_low_floor', 'Low floor'],
  ['t_low_ceiling', 'Low ceiling'],
  ['t_high', 'High threshold'],
];
export function ConfigurationEditor({
  entry,
  models,
  disabled,
  onSaved,
  onBusy,
}: {
  entry: ConfigurationEntry;
  models: RegistryModel[];
  disabled: boolean;
  onSaved: (e: ConfigurationEntry) => void;
  onBusy: (busy: boolean) => void;
}) {
  const [thresholds, setThresholds] = useState({ ...entry.thresholds.parameters }),
    [scorer, setScorer] = useState(entry.configuration.scorer.model),
    [note, setNote] = useState(entry.configuration.note_model.model),
    [question, setQuestion] = useState(entry.configuration.scorer.question_version),
    [mode, setMode] = useState<Qualification['evidence_mode']>(
      entry.qualification?.evidence_mode || 'relational',
    ),
    [kind, setKind] = useState<Qualification['data_kind']>(entry.qualification?.data_kind || 'fabricated'),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const saving = useRef(false);
  const changed =
    scorer !== entry.configuration.scorer.model ||
    question !== entry.configuration.scorer.question_version ||
    mode !== entry.qualification?.evidence_mode ||
    kind !== entry.qualification?.data_kind;
  const selectedScorer = models.find(
      (m) => m.purpose === 'scorer' && m.model === scorer && supportedModel(m),
    ),
    selectedNote = models.find((m) => m.purpose === 'note' && m.model === note && supportedModel(m));
  const calibration = entry.qualification?.calibration;
  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (saving.current || !selectedScorer || !selectedNote) return;
    saving.current = true;
    setBusy(true);
    onBusy(true);
    setError('');
    try {
      const { config_id, threshold_config_id, ...configuration } = entry.configuration;
      void config_id;
      void threshold_config_id;
      const saved = await createConfiguration({
        configuration: {
          ...configuration,
          scorer: {
            ...configuration.scorer,
            provider: selectedScorer.provider,
            model: selectedScorer.model,
            price_table: selectedScorer.price_table,
            question_version: question,
          },
          note_model: {
            ...configuration.note_model,
            provider: selectedNote.provider,
            model: selectedNote.model,
            price_table: selectedNote.price_table,
          },
        },
        thresholds,
        evidence_mode: mode,
        data_kind: kind,
      });
      onSaved(saved);
    } catch (e) {
      setError(
        e instanceof ReckonerApiError && e.status === 422
          ? 'Configuration rejected: check thresholds, supported prices, and calibration qualification. Changed scoring context cannot inherit qualification.'
          : 'Configuration save unavailable. Retrying the same document creates the same immutable version.',
      );
    } finally {
      saving.current = false;
      setBusy(false);
      onBusy(false);
    }
  }
  return (
    <section>
      <div className="section-heading">
        <h2>Create immutable version</h2>
        <span>Based on selected snapshot</span>
      </div>
      <form onSubmit={(e) => void save(e)} className="panel-body">
        <fieldset disabled={disabled || busy}>
          <div className="settings-fields">
            {fields.map(([key, label]) => (
              <label key={key} className="field">
                {label}
                <input
                  inputMode="decimal"
                  required
                  pattern="[0-9]+([.][0-9]+)?"
                  value={thresholds[key]}
                  onChange={(e) => setThresholds((t) => ({ ...t, [key]: e.target.value }))}
                />
              </label>
            ))}
            <label className="field checkbox-field">
              Amount aware
              <input
                type="checkbox"
                checked={thresholds.amount_aware}
                onChange={(e) => setThresholds((t) => ({ ...t, amount_aware: e.target.checked }))}
              />
            </label>
          </div>
          <p className="help">
            Low = clamp(review cost / amount, floor, ceiling). Flat mode uses 0.05. Equality at either
            boundary escalates. Requires 0 ≤ floor ≤ ceiling &lt; high ≤ 1; flat high &gt; 0.05.
          </p>
          <div className="settings-fields">
            {[
              ['scorer', 'Scorer model', scorer, setScorer],
              ['note', 'Note model', note, setNote],
            ].map(([purpose, label, value, setter]) => (
              <label className="field" key={purpose as string}>
                {label as string}
                <select
                  aria-label={label as string}
                  value={value as string}
                  onChange={(e) => (setter as (v: string) => void)(e.target.value)}
                >
                  {models
                    .filter((m) => m.purpose === purpose)
                    .map((m) => (
                      <option key={`${m.provider}/${m.model}`} value={m.model} disabled={!supportedModel(m)}>
                        {m.model}
                      </option>
                    ))}
                </select>
              </label>
            ))}
            <label className="field">
              Scoring question version
              <input
                required
                maxLength={256}
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
              />
            </label>
            <label className="field">
              Evidence mode
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as Qualification['evidence_mode'])}
              >
                <option>relational</option>
                <option>gds-augmented</option>
              </select>
            </label>
            <label className="field">
              Data kind
              <select value={kind} onChange={(e) => setKind(e.target.value as Qualification['data_kind'])}>
                <option>fabricated</option>
                <option>simulated-cctd</option>
              </select>
            </label>
          </div>
          <p className="help">
            Scorer prices: USD {selectedScorer?.price_table.input_per_million ?? 'unavailable'} / million
            input · note prices: USD {selectedNote?.price_table.input_per_million ?? 'unavailable'} input,{' '}
            {selectedNote?.price_table.output_per_million ?? 'unavailable'} output. Server validates exact
            pinned prices.
          </p>
          <div className="qualification">
            <strong>Calibration qualification</strong>
            {calibration ? (
              <>
                <p>
                  {calibration.calibration_id} · fit {calibration.fit_status} ·{' '}
                  {calibration.qualification.status}
                </p>
                <p>
                  Validation {calibration.qualification.validation_id} · Brier{' '}
                  {calibration.qualification.raw_brier} → {calibration.qualification.candidate_brier} · log
                  loss {calibration.qualification.raw_log_loss} →{' '}
                  {calibration.qualification.candidate_log_loss}
                </p>
                <p>
                  Threshold-only edits retain this qualification. Changes to scoring context require matching
                  qualification.
                </p>
              </>
            ) : (
              <p>
                {entry.configuration.score_mode === 'raw'
                  ? 'Raw scores · no calibration qualification'
                  : 'Qualification unavailable · a qualified configuration is required'}
              </p>
            )}
            {changed && (
              <p className="callout">
                Changed scoring context · prior calibration qualification cannot transfer. The server rejects
                a mismatched calibration ID.
              </p>
            )}
          </div>
          <button type="submit" disabled={!selectedScorer || !selectedNote}>
            Save new version
          </button>
        </fieldset>
        {error && (
          <p className="callout" role="alert">
            {error}
          </p>
        )}
      </form>
    </section>
  );
}
