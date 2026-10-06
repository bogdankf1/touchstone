import type { CaseNote } from '../../lib/reckoner-types';
type Indicator = CaseNote['risk_indicators'][number];
export type ProvenancedIndicator = Indicator & {
  evidence_ref_count: number;
  evidence_refs_sha256: string;
  evidence_refs_truncated: boolean;
};
// All three provenance fields together, or no coverage claim at all.
export function hasSourceProvenance(item: Indicator): item is ProvenancedIndicator {
  return (
    item.evidence_ref_count !== undefined &&
    item.evidence_refs_sha256 !== undefined &&
    item.evidence_refs_truncated !== undefined
  );
}
export function sourceCoverage(item: Indicator): string | null {
  if (!hasSourceProvenance(item)) return null;
  return (
    `${item.evidence_refs.length.toLocaleString('en-US')} of ` +
    `${item.evidence_ref_count.toLocaleString('en-US')} sources shown ` +
    `(${item.evidence_refs_truncated ? 'truncated' : 'complete'}; ` +
    `full list SHA-256 ${item.evidence_refs_sha256})`
  );
}
function SourceCoverage({ item }: { item: Indicator }) {
  const coverage = sourceCoverage(item);
  return coverage ? <small className="source-coverage">{coverage}</small> : null;
}
function Sources({ refs }: { refs: string[] }) {
  return (
    <div className="source-links">
      Sources:{' '}
      {refs.length
        ? refs.map((ref, i) => (
            <a href="#evidence-provenance" key={`${ref}-${i}`}>
              {ref}
            </a>
          ))
        : 'none supplied'}
    </div>
  );
}
export function StructuredNote({ note, status }: { note: CaseNote | null; status: string }) {
  if (!note)
    return (
      <section>
        <div className="section-heading">
          <h2>Structured case note</h2>
        </div>
        <p className="panel-body">
          {status === 'pending' || status === 'running'
            ? 'Note pending'
            : status === 'succeeded'
              ? 'Note unavailable'
              : 'Note failed'}{' '}
          · manual review remains available.
        </p>
      </section>
    );
  return (
    <section>
      <div className="section-heading">
        <h2>Structured case note</h2>
        <span>{note.generation_status}</span>
      </div>
      <div className="note-grid">
        <div>
          <h3>Verdict recommendation</h3>
          <p>{note.verdict_recommendation}</p>
        </div>
        <div>
          <h3>Jev confidence</h3>
          <p>
            {note.confidence.value ?? 'Unavailable'} · {note.confidence.meaning}
          </p>
        </div>
        <div className="note-wide">
          <h3>Risk indicators</h3>
          {note.risk_indicators.length ? (
            <ol>
              {note.risk_indicators.slice(0, 3).map((item) => (
                <li key={item.indicator_id}>
                  <span>{item.description}</span>
                  <small>
                    Rank {item.rank} · {item.method}
                  </small>
                  <Sources refs={item.evidence_refs} />
                  <SourceCoverage item={item} />
                </li>
              ))}
            </ol>
          ) : (
            <p>No risk indicators supplied</p>
          )}
        </div>
        <div className="note-wide">
          <h3>Entity neighbourhood</h3>
          <p>{note.entity_neighbourhood.summary}</p>
          <Sources refs={note.entity_neighbourhood.evidence_refs} />
        </div>
        <div className="note-wide">
          <h3>Comparable past cases</h3>
          {note.comparable_cases.length ? (
            note.comparable_cases.slice(0, 20).map((item, i) => (
              <div key={i}>
                <p>
                  {item.transaction_id} · {item.summary}
                </p>
                <Sources refs={item.evidence_refs} />
              </div>
            ))
          ) : (
            <p>No comparable past cases</p>
          )}
        </div>
        <div className="note-wide">
          <h3>What would change the verdict</h3>
          {note.what_would_change_verdict.length ? (
            note.what_would_change_verdict.slice(0, 20).map((item, i) => (
              <div key={i}>
                <p>{item.action}</p>
                <Sources refs={item.evidence_refs} />
              </div>
            ))
          ) : (
            <p>No further actions supplied</p>
          )}
        </div>
      </div>
      <p className="status-line">
        {note.requested_model} · prompt {note.prompt_version} · {note.note_id}
      </p>
    </section>
  );
}
