import type { Verdict } from '../../lib/reckoner-types';
export function ReviewActions({
  identity,
  setIdentity,
  disabled,
  busy,
  locked,
  onReview,
  onRetry,
  retry,
}: {
  identity: string;
  setIdentity: (value: string) => void;
  disabled: boolean;
  busy: boolean;
  locked: boolean;
  onReview: (verdict: Verdict) => void;
  onRetry: () => void;
  retry: boolean;
}) {
  return (
    <section>
      <div className="section-heading">
        <h2>Human review</h2>
        <span>a / d</span>
      </div>
      <div className="panel-body">
        <label className="field">
          Reviewer identity
          <input
            value={identity}
            maxLength={256}
            onChange={(e) => setIdentity(e.target.value)}
            disabled={locked || busy}
          />
        </label>
        <p className="help">
          Local attribution only; identity is not authenticated. Simulated reviewer identities are reserved.
        </p>
        <div className="action-row">
          <button disabled={disabled} onClick={() => onReview('approve')}>
            Approve (a)
          </button>
          <button className="decline" disabled={disabled} onClick={() => onReview('decline')}>
            Decline (d)
          </button>
          {retry && (
            <button className="secondary" disabled={busy} onClick={onRetry}>
              Retry review
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
