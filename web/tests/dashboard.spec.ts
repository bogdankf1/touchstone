import { test, expect } from '@playwright/test';

test.beforeEach(async ({ request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=baseline');
});

test('shows measured baseline context, costs, and severe missed fraud rate', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Simulated source data', { exact: true })).toBeVisible();
  await expect(page.getByText('Measured provider calls', { exact: true })).toBeVisible();
  await expect(page.getByText('DuckDB local preview')).toBeVisible();
  await expect(page.getByText('89.2%', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Missed fraud: 94 / 100', { exact: false })).toBeVisible();
  await expect(page.getByText('USD 0.458940', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('USD 7,321.795940')).toBeVisible();
  await expect(page.getByText('No compatible comparison run')).toBeVisible();
});

test('discovers workflows and keeps tenant and run filters in the URL', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Tenant').selectOption('tenant-a');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(page).toHaveURL(/tenant=tenant-a/);
  await expect(page.getByText('USD 0.300000', { exact: true }).first()).toBeVisible();
  await page.locator('select[name="run"]').selectOption('reckoner-pilot');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(page).toHaveURL(/run=reckoner-pilot/);
  await expect(page.getByText('1 failed tasks')).toBeVisible();
  await expect(page.getByText('Incomplete metric')).toBeVisible();
  await page.getByLabel('Workflow').selectOption('synthetic-ledger');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await expect(page).toHaveURL(/workflow=synthetic-ledger/);
  await expect(page.getByRole('heading', { name: 'synthetic-ledger / synthetic-run' })).toBeVisible();
  await expect(page.getByText('Fabricated measurements')).toBeVisible();
  await expect(page.getByText('Fabricated provider cost', { exact: true })).toBeVisible();
  await expect(page.getByText('Usage priced')).toHaveCount(0);
  await expect(page.getByText('Simulated source data', { exact: true })).toBeVisible();
});

test('shows explicit no snapshot, empty, stale, and missing outcome states', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=unavailable');
  await page.goto('/');
  await expect(page.getByText('No published snapshot available')).toBeVisible();
  await request.get('http://127.0.0.1:8100/__scenario?name=empty');
  await page.reload();
  await expect(page.getByText('No workflows in this snapshot')).toBeVisible();
  await request.get('http://127.0.0.1:8100/__scenario?name=stale');
  await page.reload();
  await expect(page.getByText('Latest refresh failed', { exact: true })).toBeVisible();
  await request.get('http://127.0.0.1:8100/__scenario?name=missing');
  await page.reload();
  await expect(page.getByText('5 outcomes missing')).toBeVisible();
  await expect(page.getByText('Unavailable', { exact: true }).first()).toBeVisible();
});

test('opens scoped task trace evidence and keeps accessible keyboard focus', async ({ page }) => {
  await page.goto('/');
  const link = page.getByRole('link', { name: 'View trace trace-001' });
  await link.focus();
  await expect(link).toBeFocused();
  await link.press('Enter');
  await expect(page).toHaveURL(/trace=trace-001/);
  await expect(page).toHaveURL(/tenant=tenant-a/);
  await expect(page.getByText('event-001')).toBeVisible();
});

test('escapes hostile identifiers and has a usable narrow layout', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?workflow=%3Cscript%3Ealert(1)%3C%2Fscript%3E');
  await expect(page.locator('script', { hasText: 'alert(1)' })).toHaveCount(0);
  await expect(page.getByLabel('Workflow')).toBeVisible();
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390);
});

test('captures deterministic desktop and narrow evidence', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/');
  await expect(page.getByText('Simulated source data', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'artifacts/dashboard-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'artifacts/dashboard-narrow.png', fullPage: true });
});

test('does not present a partial tenant discovery as an empty run list', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=runs-error');
  await page.goto('/');
  await expect(page.getByText('Run discovery incomplete')).toBeVisible();
  await expect(page.getByText('No runs for this selection')).toHaveCount(0);
  await expect(page.getByText('89.2%', { exact: true })).toHaveCount(0);
});

for (const scenario of ['generation-runs', 'generation-summary', 'generation-tasks', 'generation-trace']) {
  test(`does not combine snapshots when ${scenario} changes generation`, async ({ page, request }) => {
    await request.get(`http://127.0.0.1:8100/__scenario?name=${scenario}`);
    await page.goto(scenario === 'generation-trace' ? '/?trace=trace-001&trace_tenant=tenant-a' : '/');
    await expect(page.getByText('Snapshot changed during read')).toBeVisible();
    await expect(page.getByText('89.2%', { exact: true })).toHaveCount(0);
    await expect(page.getByText('event-001')).toHaveCount(0);
  });
}

test('labels unknown measurement and source provenance without guessing', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=unknown-provenance');
  await page.goto('/');
  await expect(page.getByText('Measurement mode unknown')).toBeVisible();
  await expect(page.getByText('Source simulation unknown', { exact: true })).toBeVisible();
  await expect(page.getByText('Measured calls on simulated data')).toHaveCount(0);
  await expect(page.getByText('Simulated source data', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Usage priced')).toHaveCount(0);
});

test('does not infer real customer data from a false simulation flag', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=nonsimulated-provenance');
  await page.goto('/');
  await expect(page.getByText('Source origin unverified', { exact: true })).toBeVisible();
  await expect(page.getByText('Simulated source data', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Measured calls on simulated data')).toHaveCount(0);
});

test('keeps online cost visible when offline billing is incomplete', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=offline-pending');
  await page.goto('/');
  await expect(page.getByText('Online model cost', { exact: true })).toBeVisible();
  await expect(page.getByText('USD 0.458940', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('row', { name: /Offline model cost/ })).toContainText('Unavailable');
  await expect(page.getByRole('row', { name: /Total provider spend/ })).toContainText('Unavailable');
});

test('shows compatible simulated comparison and arm provenance', async ({page, request}) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=comparison');
  await page.goto('/?compare=reckoner-pilot');
  await expect(page.getByRole('heading',{name:'Baseline / current comparison'})).toBeVisible();
  await expect(page.getByText('CPST change: USD -0.500000')).toBeVisible();
  await expect(page.getByText('Simulated comparison fixture')).toBeVisible();
  await page.getByText('Arm versions and call identities').last().click();
  await expect(page.getByText('gds-augmented', {exact:false})).toBeVisible();
});
for (const scenario of ['comparison-ineligible','comparison-generation']) {
  test(`hides delta for ${scenario}`, async ({page,request}) => {
    await request.get(`http://127.0.0.1:8100/__scenario?name=${scenario}`);
    await page.goto('/?compare=reckoner-pilot');
    await expect(page.getByText('CPST change:',{exact:false})).toHaveCount(0);
    await expect(page.getByText(scenario==='comparison-generation'?'Comparison snapshot changed':'Comparison ineligible')).toBeVisible();
  });
}
