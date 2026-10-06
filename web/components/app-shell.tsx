import Link from 'next/link';
export function AppShell({
  children,
  surface = 'Runs',
}: {
  children: React.ReactNode;
  surface?: 'Runs' | 'Review' | 'Settings';
}) {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">T</span>
          <span>TOUCHSTONE</span>
        </div>
        <nav aria-label="Primary">
          {[
            ['Runs', '/'],
            ['Review', '/review'],
            ['Settings', '/settings'],
          ].map(([name, href]) => (
            <Link
              key={name}
              href={href}
              className={surface === name ? 'active' : undefined}
              aria-current={surface === name ? 'page' : undefined}
            >
              {name}
            </Link>
          ))}
        </nav>
        <div className="sidebar-foot">LOCAL PREVIEW</div>
      </aside>
      <main id="main">
        <header className="topbar">
          <span>
            {surface === 'Runs' ? 'Measurement' : 'Reckoner'} / {surface}
          </span>
          <span>Touchstone v1</span>
        </header>
        <div className="main-inner">{children}</div>
      </main>
    </div>
  );
}
