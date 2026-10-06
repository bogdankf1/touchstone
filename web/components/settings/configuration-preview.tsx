'use client';
import { useRef, useState } from 'react';
import { previewConfiguration } from '../../lib/reckoner-api';
import type { ConfigurationPreview as Preview } from '../../lib/reckoner-types';
export function ConfigurationPreview({
  tenant,
  configId,
  disabled,
}: {
  tenant: string;
  configId: string;
  disabled: boolean;
}) {
  const [run, setRun] = useState(''),
    [preview, setPreview] = useState<Preview | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const sending = useRef(false);
  async function show() {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setError('');
    setPreview(null);
    try {
      setPreview(await previewConfiguration(tenant, run, configId));
    } catch {
      setError('Stored-score preview unavailable. Check the run ID and retry.');
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }
  return (
    <section>
      <div className="section-heading">
        <h2>Score-only preview · estimate</h2>
        <span>No provider calls</span>
      </div>
      <div className="panel-body">
        <p className="help">
          Replays stored effective scores with this saved configuration. Covers completed decisions only;
          excludes unexecuted tasks. Note and model costs not estimated.
        </p>
        <label className="field">
          Preview run ID
          <input
            value={run}
            maxLength={256}
            onChange={(e) => {
              setRun(e.target.value);
              setPreview(null);
            }}
            disabled={busy}
          />
        </label>
        <button disabled={disabled || busy || !run.trim()} onClick={() => void show()}>
          Preview stored scores
        </button>
        {busy && <p role="status">Replaying stored scores…</p>}
        {error && <p role="alert">{error}</p>}
        {preview &&
          (preview.status === 'incompatible_scores' ? (
            <p className="callout">
              Incompatible scoring context · stored scores cannot estimate this candidate. No changed-model
              prediction or cost forecast.
            </p>
          ) : (
            <>
              <p>{preview.completed_decisions} completed decisions</p>
              <dl className="preview-counts">
                {Object.entries(preview.counts || {}).map(([name, count]) => (
                  <div key={name}>
                    <dt>{name}</dt>
                    <dd>{count}</dd>
                  </div>
                ))}
              </dl>
              <p className="help">
                {preview.missing_scores ?? 'Unknown'} missing scores conservatively escalated ·{' '}
                {preview.provider_calls} provider calls
              </p>
            </>
          ))}
      </div>
    </section>
  );
}
