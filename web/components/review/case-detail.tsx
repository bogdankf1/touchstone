import type { CaseDetail as Detail } from '../../lib/reckoner-types';
import { StructuredNote, hasSourceProvenance } from './structured-note';
import { GraphViewer } from './graph-viewer';
export function CaseDetail({ detail }: { detail: Detail }) {
  return (
    <>
      <div className="case-heading">
        <h2>{detail.case_id}</h2>
        <span>
          {detail.status} · version {detail.version}
        </span>
        <p>
          {detail.transaction_id} · {detail.run_id} · {detail.occurred_at}
        </p>
      </div>
      {detail.degraded && (
        <p className="callout">
          Degraded evidence: {detail.degraded_reason || 'reason unavailable'}. Effective score{' '}
          {detail.effective_probability ?? 'unavailable'}.
        </p>
      )}
      {detail.review && (
        <div className="refresh">
          <strong>Resolved by {detail.review.reviewer_id}</strong>
          <span>
            {detail.review.verdict} · {detail.review.reviewer_type} · {detail.review.reviewed_at}
          </span>
          <span>Captured recommendation: {detail.review.recommendation ?? 'unavailable'}</span>
        </div>
      )}
      {detail.available_before_review === false && detail.note && (
        <p className="callout">Late note · available after review. Captured recommendation is immutable.</p>
      )}
      <StructuredNote note={detail.note} status={detail.note_status} />
      <GraphViewer graph={detail.graph} tenant={detail.tenant_id} />
      <section id="evidence-provenance">
        <div className="section-heading">
          <h2>Evidence provenance</h2>
        </div>
        <dl className="provenance">
          <dt>Evidence</dt>
          <dd>{detail.evidence_id}</dd>
          <dt>Configuration</dt>
          <dd>{detail.config_id}</dd>
          <dt>Coverage</dt>
          <dd>
            {detail.coverage.status} ·{' '}
            {detail.coverage.missing.length
              ? `Missing: ${detail.coverage.missing.join(', ')}`
              : 'No missing evidence reported'}
          </dd>
          <dt>Effective probability</dt>
          <dd>
            {detail.effective_probability ?? 'Unavailable'} · low {detail.effective_low_threshold} · high{' '}
            {detail.effective_high_threshold}
          </dd>
          {Object.entries(detail.cutoffs).map(([key, value]) => (
            <div className="provenance-row" key={key}>
              <dt>{key}</dt>
              <dd>{value ?? 'Unavailable cutoff'}</dd>
            </div>
          ))}
          {(detail.note?.risk_indicators ?? []).filter(hasSourceProvenance).map((item) => (
            <div className="provenance-row" key={`sources-${item.indicator_id}`}>
              <dt>{item.indicator_id} sources</dt>
              <dd>
                {item.evidence_ref_count.toLocaleString('en-US')} references ·{' '}
                {item.evidence_refs.length.toLocaleString('en-US')} shown in note
                {item.evidence_refs_truncated ? ' (truncated)' : ''} · full list SHA-256{' '}
                {item.evidence_refs_sha256}
              </dd>
            </div>
          ))}
          {Object.entries(detail.source_snapshot_ids).map(([key, value]) => (
            <div className="provenance-row" key={key}>
              <dt>{key} snapshot</dt>
              <dd>{value ?? 'Unavailable snapshot'}</dd>
            </div>
          ))}
        </dl>
      </section>
    </>
  );
}
