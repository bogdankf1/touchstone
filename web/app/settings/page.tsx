'use client';
import { useEffect, useRef, useState } from 'react';
import { AppShell } from '../../components/app-shell';
import { ConfigurationEditor } from '../../components/settings/configuration-editor';
import { ConfigurationHistoryView } from '../../components/settings/configuration-history';
import { ConfigurationPreview } from '../../components/settings/configuration-preview';
import { listConfigurations, activateConfiguration, ReckonerApiError } from '../../lib/reckoner-api';
import type { ConfigurationHistory } from '../../lib/reckoner-types';
type Pending = { tenant: string; configId: string; version: number; key: string };
export default function SettingsPage() {
  const [tenant, setTenant] = useState('tenant-a'),
    [history, setHistory] = useState<ConfigurationHistory | null>(null),
    [selected, setSelected] = useState(''),
    [busy, setBusy] = useState(false),
    [saving, setSaving] = useState(false),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(''),
    [retry, setRetry] = useState(false),
    [conflict, setConflict] = useState(false),
    [revision, setRevision] = useState(0);
  const pending = useRef<Pending | null>(null),
    sending = useRef(false);
  useEffect(() => {
    let cancelled = false;
    listConfigurations(tenant)
      .then((h) => {
        if (!cancelled) {
          setHistory(h);
          setSelected(h.activation.config_id || h.items[0]?.config_id || '');
        }
      })
      .catch(() => {
        if (!cancelled) setError('Settings unavailable. Reload when the operational API is available.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tenant, revision]);
  function resetSettings() {
    setLoading(true);
    setHistory(null);
    setError('');
    setConflict(false);
  }
  async function refresh() {
    setBusy(true);
    try {
      setHistory(await listConfigurations(tenant));
      setConflict(false);
      setError('');
    } catch {
      setError('Current defaults unavailable. Retry refresh before activating.');
      setConflict(true);
    } finally {
      setBusy(false);
    }
  }
  async function activate(p: Pending) {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setError('');
    try {
      await activateConfiguration(p.tenant, p.configId, p.version, p.key);
      const h = await listConfigurations(p.tenant);
      setHistory(h);
      pending.current = null;
      setRetry(false);
      setConflict(false);
    } catch (e) {
      if (e instanceof ReckonerApiError && (e.status === 409 || e.status === 422)) {
        pending.current = null;
        setRetry(false);
        setConflict(true);
        setError(
          e.status === 409
            ? 'Activation conflict: current defaults changed. Refresh current defaults before explicitly activating your selected candidate.'
            : 'Activation rejected: saved qualification or registry no longer matches. Refresh current defaults.',
        );
      } else {
        setRetry(true);
        setError(
          'Activation response unavailable. Retry using the original activation key and expected version.',
        );
      }
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }
  const locked = busy || saving || retry;
  const entry = history?.items.find((e) => e.config_id === selected);
  return (
    <AppShell surface="Settings">
      <div className="run-heading">
        <div>
          <span className="eyebrow">RECKONER / CONFIGURATION</span>
          <h1>Settings</h1>
          <p>Simulated source data · immutable versions · future-run defaults</p>
        </div>
      </div>
      <div className="operational-filters">
        <label className="field">
          Tenant
          <select
            value={tenant}
            disabled={locked}
            onChange={(e) => {
              resetSettings();
              setTenant(e.target.value);
            }}
          >
            <option>tenant-a</option>
            <option>tenant-b</option>
          </select>
        </label>
        <button
          className="secondary"
          disabled={locked}
          onClick={() => {
            resetSettings();
            setRevision((r) => r + 1);
          }}
        >
          Reload settings
        </button>
      </div>
      {error && (
        <p className="callout" role="alert">
          {error}
        </p>
      )}
      {loading ? (
        <p role="status">Loading configurations…</p>
      ) : (
        history && (
          <>
            <div className="refresh">
              <strong>
                Current defaults: {history.activation.config_id ?? 'none'} · activation version{' '}
                {history.activation.version}
              </strong>
              <span>
                Activation affects future runs only. Active and historical runs retain their snapshots.
              </span>
            </div>
            {!history.items.length ? (
              <p className="empty">
                No configuration is available for this tenant. Settings need an initialized workload context.
              </p>
            ) : (
              <>
                <ConfigurationHistoryView
                  history={history}
                  selected={selected}
                  onSelect={setSelected}
                  disabled={locked}
                />
                {entry && (
                  <>
                    <div className="action-row activation-actions">
                      <button
                        disabled={locked || conflict}
                        onClick={() => {
                          const p = {
                            tenant,
                            configId: selected,
                            version: history.activation.version,
                            key: crypto.randomUUID(),
                          };
                          pending.current = p;
                          void activate(p);
                        }}
                      >
                        Activate for future runs
                      </button>
                      {retry && (
                        <button
                          className="secondary"
                          disabled={busy}
                          onClick={() => {
                            if (pending.current) void activate(pending.current);
                          }}
                        >
                          Retry activation
                        </button>
                      )}
                      <button className="secondary" disabled={locked} onClick={() => void refresh()}>
                        Refresh current defaults
                      </button>
                    </div>
                    <div className="settings-layout">
                      <ConfigurationEditor
                        key={`${tenant}/${selected}`}
                        entry={entry}
                        models={history.models}
                        disabled={locked}
                        onBusy={setSaving}
                        onSaved={(e) => {
                          setHistory((h) =>
                            h
                              ? {
                                  ...h,
                                  items: [...h.items.filter((item) => item.config_id !== e.config_id), e],
                                }
                              : h,
                          );
                          setSelected(e.config_id);
                        }}
                      />
                      <ConfigurationPreview
                        key={`preview/${tenant}/${selected}`}
                        tenant={tenant}
                        configId={selected}
                        disabled={locked}
                      />
                    </div>
                  </>
                )}
              </>
            )}
          </>
        )
      )}
    </AppShell>
  );
}
