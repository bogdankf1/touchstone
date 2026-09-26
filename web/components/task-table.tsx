import Link from 'next/link';
import type { Filters, Page, Task, Trace } from '../lib/types';

function query(filters: Filters, extra: Record<string, string>) {
  return `/?${new URLSearchParams({ workflow: filters.workflow, run: filters.run, tenant: filters.tenant, ...extra })}`;
}

export function TaskTable({
  tasks,
  filters,
  trace,
}: {
  tasks: Page<Task>;
  filters: Filters;
  trace: Trace | null;
}) {
  return (
    <section aria-labelledby="tasks-title">
      <div className="section-heading">
        <h2 id="tasks-title">Tasks & trace evidence</h2>
        <span>
          {tasks.total} tasks · page {tasks.page}
        </span>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Task</th>
              <th scope="col">Tenant</th>
              <th scope="col">Status</th>
              <th scope="col">Latency</th>
              <th scope="col">Trace</th>
            </tr>
          </thead>
          <tbody>
            {tasks.items.map((task) => (
              <tr key={`${task.tenant_id}-${task.task_id}`}>
                <th scope="row">{task.task_id}</th>
                <td>{task.tenant_id}</td>
                <td>
                  {task.terminal_status || 'Unknown'}
                  {task.incomplete ? ' · incomplete' : ''}
                </td>
                <td>{task.latency_ms === null ? 'Unavailable' : `${task.latency_ms.toFixed(1)} ms`}</td>
                <td>
                  {task.nodes
                    .filter((node) => node.trace_id)
                    .map((node) => (
                      <Link
                        key={`${node.node_name}-${node.trace_id}`}
                        href={query(filters, { trace: node.trace_id!, trace_tenant: task.tenant_id })}
                        aria-label={`View trace ${node.trace_id}`}
                      >
                        {node.trace_id}
                      </Link>
                    ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <nav className="pager" aria-label="Task pages">
        {tasks.page > 1 && <Link href={query(filters, { page: String(tasks.page - 1) })}>Previous</Link>}
        {tasks.page * tasks.page_size < tasks.total && (
          <Link href={query(filters, { page: String(tasks.page + 1) })}>Next</Link>
        )}
      </nav>
      {trace && (
        <div className="trace-panel">
          <div className="section-heading">
            <h3>Trace {trace.trace_id}</h3>
            <span>{trace.total} events</span>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Event</th>
                  <th scope="col">Node</th>
                  <th scope="col">Kind</th>
                  <th scope="col">Received</th>
                </tr>
              </thead>
              <tbody>
                {trace.events.map((event) => (
                  <tr key={event.event_id}>
                    <th scope="row">{event.event_id}</th>
                    <td>{event.node_name}</td>
                    <td>{event.event_kind}</td>
                    <td>{event.received_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
