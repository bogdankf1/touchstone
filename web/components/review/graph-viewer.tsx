import type { GraphSnapshot } from '../../lib/reckoner-types';
export function GraphViewer({ graph, tenant }: { graph: GraphSnapshot; tenant: string }) {
  const nodes = graph.nodes.slice(0, 100);
  const visible = nodes.slice(0, 24);
  const positions = new Map(
    visible.map((n, i) => [n.id, { x: 80 + (i % 4) * 150, y: 50 + Math.floor(i / 4) * 100 }]),
  );
  const height = Math.max(120, Math.ceil(visible.length / 4) * 100);
  return (
    <section>
      <div className="section-heading">
        <h2>Graph neighbourhood</h2>
        <span>Persisted snapshot</span>
      </div>
      {graph.status === 'unavailable' ? (
        <p className="panel-body">
          Graph evidence unavailable · this is snapshot availability, not live service health.
        </p>
      ) : !nodes.length ? (
        <p className="panel-body">No graph neighbours · available empty snapshot.</p>
      ) : (
        <>
          <svg
            className="graph"
            viewBox={`0 0 620 ${height}`}
            role="img"
            aria-label="Bounded entity neighbourhood"
          >
            <title>Entity relationships from persisted evidence</title>
            {graph.edges.slice(0, 200).map((e) => {
              const a = positions.get(e.source),
                b = positions.get(e.target);
              return a && b ? <line key={e.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#a4b7ca" /> : null;
            })}
            {visible.map((n) => {
              const p = positions.get(n.id)!;
              return (
                <g key={n.id}>
                  <title>
                    {n.identity} · {n.kind} · {n.tenant_id}
                  </title>
                  <rect
                    x={p.x - 65}
                    y={p.y - 23}
                    width="130"
                    height="46"
                    rx="5"
                    fill={n.tenant_id === tenant ? '#eaf2fa' : '#fff0d7'}
                    stroke="#9fb6ce"
                  />
                  <text x={p.x} y={p.y - 3} textAnchor="middle" fontSize="11" fill="#243b55">
                    {n.identity.slice(0, 20)}
                  </text>
                  <text x={p.x} y={p.y + 13} textAnchor="middle" fontSize="9" fill="#536a82">
                    {n.kind}
                  </text>
                </g>
              );
            })}
          </svg>
          <div className="graph-identities">
            {nodes.map((n) => (
              <div key={n.id}>
                <strong>{n.identity}</strong>
                <span>
                  {n.kind} · {n.tenant_id === tenant ? n.tenant_id : `Cross-tenant · ${n.tenant_id}`}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      {(graph.truncated || graph.nodes.length > 24 || graph.edges.length > 200) && (
        <p className="callout">
          Truncated graph · {visible.length} drawn / {graph.total_nodes} recorded nodes; {graph.total_edges}{' '}
          recorded edges. Text list bounded to 100 nodes; drawing bounded to 24.
        </p>
      )}
    </section>
  );
}
