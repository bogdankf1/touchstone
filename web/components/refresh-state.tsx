import type { Metadata } from '../lib/types';

export function RefreshState({ metadata }: { metadata: Metadata }) {
  const state = metadata.latest_refresh.state;
  return (
    <div className="refresh" role="status">
      <span className="dot" aria-hidden="true" />
      <strong>
        {state === 'failed'
          ? 'Latest refresh failed'
          : state === 'published_with_warning'
            ? 'Published with warnings'
            : state === 'succeeded'
              ? 'Latest refresh succeeded'
              : 'Refresh state unknown'}
      </strong>
      <span>{metadata.warehouse}</span>
      <span>Published {metadata.published_at || 'unknown'}</span>
      <span>Generation {metadata.generation}</span>
      {metadata.latest_refresh.reason && <span>{metadata.latest_refresh.reason}</span>}
    </div>
  );
}
