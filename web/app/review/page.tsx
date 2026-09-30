'use client';
import { useEffect, useRef, useState } from 'react';
import { AppShell } from '../../components/app-shell';
import { CaseQueue } from '../../components/review/case-queue';
import { CaseDetail } from '../../components/review/case-detail';
import { ReviewActions } from '../../components/review/review-actions';
import { getCase, listCases, submitReview, ReckonerApiError } from '../../lib/reckoner-api';
import type {
  CaseDetail as Detail,
  CasePage,
  CaseStatus,
  ReviewAction,
  Verdict,
} from '../../lib/reckoner-types';
type Pending = { action: ReviewAction; key: string; version: number };
export default function ReviewPage() {
  const [tenant, setTenant] = useState('tenant-a'),
    [status, setStatus] = useState<CaseStatus>('all'),
    [run, setRun] = useState(''),
    [queue, setQueue] = useState<CasePage>({ items: [], next_cursor: null, limit: 50 }),
    [selected, setSelected] = useState(''),
    [detail, setDetail] = useState<Detail | null>(null),
    [identity, setIdentity] = useState(''),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [retry, setRetry] = useState(false),
    [conflict, setConflict] = useState(false),
    [revision, setRevision] = useState(0);
  const pending = useRef<Pending | null>(null),
    sending = useRef(false);
  useEffect(() => {
    let cancelled = false;
    listCases({ tenantId: tenant, status, runId: run })
      .then((q) => {
        if (!cancelled) {
          setQueue(q);
          setSelected(q.items[0]?.case_id || '');
        }
      })
      .catch(() => {
        if (!cancelled) {
          setQueue({ items: [], next_cursor: null, limit: 50 });
          setError('Cases unavailable. Retry when the API is available.');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tenant, status, run, revision]);
  useEffect(() => {
    let cancelled = false;
    if (selected)
      getCase(tenant, selected)
        .then((d) => {
          if (!cancelled) setDetail(d);
        })
        .catch(() => {
          if (!cancelled) setError('Case detail unavailable. Refresh case to retry.');
        });
    return () => {
      cancelled = true;
    };
  }, [tenant, selected]);
  function resetQueue() {
    setLoading(true);
    setDetail(null);
    setSelected('');
    setError('');
    setConflict(false);
  }
  function selectCase(id: string) {
    if (id !== selected) setDetail(null);
    setSelected(id);
    setConflict(false);
    setError('');
  }
  const validIdentity =
    !!identity.trim() &&
    identity === identity.trim() &&
    !/^simulat/i.test(identity) &&
    !/[\x00-\x1f\x7f]/.test(identity);
  const disabled = busy || retry || conflict || !validIdentity || !detail || detail.status === 'resolved';
  async function send(p: Pending) {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setError('');
    try {
      await submitReview(p.action, p.key, p.version);
      const d = await getCase(p.action.tenant_id, p.action.case_id);
      pending.current = null;
      setRetry(false);
      setDetail(d);
      setQueue((q) => ({ ...q, items: q.items.map((c) => (c.case_id === d.case_id ? d : c)) }));
    } catch (e) {
      if (e instanceof ReckonerApiError && e.status === 409) {
        pending.current = null;
        setRetry(false);
        setConflict(true);
        setError('Review conflict: another reviewer or case version won. Refresh case before continuing.');
      } else if (e instanceof ReckonerApiError && e.status === 422) {
        pending.current = null;
        setRetry(false);
        setError('Review rejected. Check reviewer identity and case version.');
      } else {
        setRetry(true);
        setError(
          'Review response unavailable. Retry the original action with its original identity and key.',
        );
      }
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }
  function review(verdict: Verdict) {
    if (disabled || sending.current || !detail) return;
    const p = {
      action: { tenant_id: tenant, case_id: detail.case_id, reviewer_id: identity, verdict },
      key: crypto.randomUUID(),
      version: detail.version,
    };
    pending.current = p;
    void send(p);
  }
  function navigate(delta: number) {
    if (busy || retry) return;
    const i = queue.items.findIndex((c) => c.case_id === selected);
    const next = queue.items[Math.max(0, Math.min(queue.items.length - 1, i + delta))];
    if (next) {
      setError('');
      setConflict(false);
      selectCase(next.case_id);
    }
  }
  useEffect(() => {
    function keyboard(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      if (
        e.repeat ||
        e.altKey ||
        e.ctrlKey ||
        e.metaKey ||
        e.shiftKey ||
        target.closest('input,textarea,select,button,a,[contenteditable="true"],[role="textbox"]')
      )
        return;
      if (['j', 'k', 'a', 'd'].includes(e.key)) {
        e.preventDefault();
        if (e.key === 'j') navigate(1);
        else if (e.key === 'k') navigate(-1);
        else review(e.key === 'a' ? 'approve' : 'decline');
      }
    }
    document.addEventListener('keydown', keyboard);
    return () => document.removeEventListener('keydown', keyboard);
  });
  async function refresh() {
    setBusy(true);
    try {
      if (selected) setDetail(await getCase(tenant, selected));
      setConflict(false);
      setError('');
    } catch {
      setError('Case detail unavailable. Retry refresh.');
    } finally {
      setBusy(false);
    }
  }
  async function more() {
    setBusy(true);
    try {
      const q = await listCases({
        tenantId: tenant,
        status,
        runId: run,
        cursor: queue.next_cursor || undefined,
      });
      setQueue((old) => ({ ...q, items: [...old.items, ...q.items] }));
    } catch {
      setError('More cases unavailable. Retry loading.');
    } finally {
      setBusy(false);
    }
  }
  return (
    <AppShell surface="Review">
      <div className="run-heading">
        <div>
          <span className="eyebrow">RECKONER / HUMAN REVIEW</span>
          <h1>Reviewer console</h1>
          <p>Simulated source data · persisted evidence · j / k navigate, a / d decide</p>
        </div>
      </div>
      <div className="operational-filters">
        <label className="field">
          Tenant
          <select
            value={tenant}
            disabled={busy || retry}
            onChange={(e) => {
              resetQueue();
              setTenant(e.target.value);
            }}
          >
            <option>tenant-a</option>
            <option>tenant-b</option>
          </select>
        </label>
        <label className="field">
          Case status
          <select
            value={status}
            disabled={busy || retry}
            onChange={(e) => {
              resetQueue();
              setStatus(e.target.value as CaseStatus);
            }}
          >
            {['all', 'open', 'resolved', 'note_pending', 'note_failed', 'degraded'].map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <label className="field">
          Run ID (optional)
          <input
            value={run}
            disabled={busy || retry}
            onChange={(e) => {
              resetQueue();
              setRun(e.target.value);
            }}
            maxLength={256}
          />
        </label>
        <button
          className="secondary"
          disabled={busy || retry}
          onClick={() => {
            resetQueue();
            setRevision((r) => r + 1);
          }}
        >
          Reload queue
        </button>
      </div>
      {error && (
        <p role="alert" className="callout">
          {error}
        </p>
      )}
      {loading ? (
        <p role="status">Loading cases…</p>
      ) : (
        <div className="review-layout">
          <CaseQueue
            items={queue.items}
            selected={selected}
            onSelect={selectCase}
            disabled={busy || retry}
            more={!!queue.next_cursor}
            onMore={() => void more()}
          />
          <div>
            {selected && !detail && <p role="status">Loading case detail…</p>}
            {detail && (
              <>
                <CaseDetail detail={detail} />
                <ReviewActions
                  identity={identity}
                  setIdentity={setIdentity}
                  disabled={disabled}
                  busy={busy}
                  locked={retry}
                  retry={retry}
                  onReview={review}
                  onRetry={() => {
                    if (pending.current) void send(pending.current);
                  }}
                />
                <button className="secondary" disabled={busy || retry} onClick={() => void refresh()}>
                  Refresh case
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </AppShell>
  );
}
