import { useEffect, useRef } from 'react';
import type { CaseSummary } from '../../lib/reckoner-types';
function money(minor: string) {
  const digits = minor.padStart(3, '0');
  return `${digits.slice(0, -2)}.${digits.slice(-2)}`;
}
export function CaseQueue({
  items,
  selected,
  onSelect,
  disabled,
  more,
  onMore,
}: {
  items: CaseSummary[];
  selected: string;
  onSelect: (id: string) => void;
  disabled: boolean;
  more: boolean;
  onMore: () => void;
}) {
  const queue = useRef<HTMLDivElement>(null);
  useEffect(() => {
    function keepSelectedVisible() {
      const container = queue.current;
      const selectedRow = container?.querySelector<HTMLButtonElement>('[aria-current="true"]');
      if (!container || !selectedRow) return;
      const row = selectedRow.getBoundingClientRect(),
        bounds = container.getBoundingClientRect();
      if (row.top < bounds.top) container.scrollTop += row.top - bounds.top - 4;
      else if (row.bottom > bounds.bottom) container.scrollTop += row.bottom - bounds.bottom + 4;
    }
    keepSelectedVisible();
    window.addEventListener('resize', keepSelectedVisible);
    return () => window.removeEventListener('resize', keepSelectedVisible);
  }, [selected]);
  return (
    <section className="case-queue" aria-label="Case queue">
      <div className="section-heading">
        <h2>Case queue</h2>
        <span>{items.length} loaded · j / k</span>
      </div>
      <div className="queue-items" ref={queue}>
        {items.map((item) => (
          <button
            key={item.case_id}
            aria-current={selected === item.case_id ? 'true' : undefined}
            disabled={disabled}
            onClick={() => onSelect(item.case_id)}
          >
            <strong>{item.case_id}</strong>
            <span>
              {item.currency} {money(item.amount_minor)}
            </span>
            <small>
              {item.status} · note {item.note_status}
              {item.degraded ? ' · degraded' : ''}
            </small>
          </button>
        ))}
      </div>
      {!items.length && <p className="panel-body">No cases match</p>}
      {more && (
        <button className="secondary load-more" disabled={disabled} onClick={onMore}>
          Load more cases
        </button>
      )}
    </section>
  );
}
