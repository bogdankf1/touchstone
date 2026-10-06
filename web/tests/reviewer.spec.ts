import { test, expect } from '@playwright/test';
test.beforeEach(async ({ request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=review');
});
test('clicked queue supports shortcuts while editable controls do not submit', async ({ page, request }) => {
  await page.goto('/review');
  await expect(page.getByRole('heading', { name: 'case-001', exact: true })).toBeVisible();
  await page.getByLabel('Reviewer identity').fill('alex');
  await page.getByLabel('Reviewer identity').press('j');
  await expect(page.getByRole('heading', { name: 'case-001', exact: true })).toBeVisible();
  await page.getByLabel('Reviewer identity').fill('alex');
  await page.getByRole('button', { name: /case-001/ }).click();
  await page.keyboard.press('j');
  await expect(page.getByRole('heading', { name: 'case-002', exact: true })).toBeVisible();
  await page.keyboard.press('k');
  await expect(page.getByRole('heading', { name: 'case-001', exact: true })).toBeVisible();
  await page.keyboard.press('a');
  await page.keyboard.press('a');
  await expect(page.getByText('Resolved by alex')).toBeVisible();
  const w = await (await request.get('http://127.0.0.1:8100/__writes')).json();
  expect(w).toHaveLength(1);
  expect(w[0]).toMatchObject({ reviewer_id: 'alex', expected_version: 1, verdict: 'approve' });
  const q = page.getByRole('button', { name: /case-002/ });
  await q.focus();
  await expect(q).toBeFocused();
  expect(await q.evaluate((el) => getComputedStyle(el).outlineStyle)).toBe('solid');
});
test('structured note and bounded graph render safe text with source provenance', async ({
  page,
  request,
}) => {
  await page.goto('/review');
  for (const name of [
    'Verdict recommendation',
    'Jev confidence',
    'Risk indicators',
    'Entity neighbourhood',
    'Comparable past cases',
    'What would change the verdict',
  ])
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  await expect(page.getByText('<img src=x onerror=alert(1)>', { exact: true })).toBeVisible();
  await expect(page.locator('img')).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'evidence-a', exact: true }).first()).toHaveAttribute(
    'href',
    '#evidence-provenance',
  );
  await expect(page.getByText('Cross-tenant · tenant-b')).toBeVisible();
  await expect(page.getByText(/Truncated graph/)).toBeVisible();
  await expect(page.getByText('No comparable past cases')).toBeVisible();
  await expect(page.getByText(/Simulated source data/)).toBeVisible();
  const payload = await (
    await request.get('http://127.0.0.1:8100/v1/case?tenant_id=tenant-a&case_id=case-001')
  ).text();
  expect(payload).not.toMatch(/oracle|fraud_label|api_key/i);
});
test('truncated indicator sources state the full count and list hash', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=truncated-sources');
  await page.goto('/review');
  const full = 'a'.repeat(64);
  await expect(
    page.getByText(`32 of 2,944 sources shown (truncated; full list SHA-256 ${full})`),
  ).toBeVisible();
  await expect(
    page.getByText(`5 of 5 sources shown (complete; full list SHA-256 ${'b'.repeat(64)})`),
  ).toBeVisible();
  const provenance = page.locator('#evidence-provenance');
  await expect(provenance.getByText('merchant-exposure sources', { exact: true })).toBeVisible();
  await expect(
    provenance.getByText(`2,944 references · 32 shown in note (truncated) · full list SHA-256 ${full}`),
  ).toBeVisible();
  await expect(provenance.getByText('amount-ratio sources')).toHaveCount(0);
  await expect(page.getByText(/of 6 sources shown/)).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390);
});
for (const [s, label] of [
  ['note-failed', 'Note failed'],
  ['note-pending', 'Note pending'],
  ['degraded', 'scorer unavailable'],
  ['no-graph', 'Graph evidence unavailable'],
  ['empty-graph', 'No graph neighbours'],
  ['review-empty', 'No cases match'],
  ['reckoner-unavailable', 'Cases unavailable'],
  ['late-note', 'Late note'],
])
  test(`shows ${s} clearly`, async ({ page, request }) => {
    await request.get(`http://127.0.0.1:8100/__scenario?name=${s}`);
    await page.goto('/review');
    await expect(page.getByText(label, { exact: false }).first()).toBeVisible();
    if (s === 'late-note') {
      await expect(page.getByText('Captured recommendation: unavailable')).toBeVisible();
      await expect(page.getByRole('button', { name: 'Approve (a)' })).toBeDisabled();
    } else if (['note-failed', 'degraded'].includes(s)) {
      await page.getByLabel('Reviewer identity').fill('alex');
      await expect(page.getByRole('button', { name: 'Decline (d)' })).toBeEnabled();
    }
  });
test('reserved actor blocked and stale review visible', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=review-stale');
  await page.goto('/review');
  await page.getByLabel('Reviewer identity').fill('simulated-reviewer');
  await expect(page.getByRole('button', { name: 'Approve (a)' })).toBeDisabled();
  await page.getByLabel('Reviewer identity').fill('alex');
  await page.getByRole('button', { name: 'Approve (a)' }).click();
  await expect(page.locator('main').getByRole('alert')).toContainText('conflict');
});
test('lost review retries same action', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=review-lost');
  await page.goto('/review');
  await page.getByLabel('Reviewer identity').fill('alex');
  await page.getByRole('button', { name: 'Decline (d)' }).click();
  await page.getByRole('button', { name: 'Retry review' }).click();
  await expect(page.getByText('Resolved by alex')).toBeVisible();
  const w = await (await request.get('http://127.0.0.1:8100/__writes')).json();
  expect(w[0]).toEqual(w[1]);
});
test('crowded queue paginates and mobile fits viewport', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=crowded');
  await page.goto('/review');
  await page.getByRole('button', { name: 'Load more cases' }).click();
  await page.getByRole('button', { name: /case-100/ }).click();
  await expect(page.getByRole('heading', { name: 'case-100', exact: true })).toBeVisible();
  await page.screenshot({ path: 'artifacts/reviewer-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390);
  await page.screenshot({ path: 'artifacts/reviewer-mobile.png', fullPage: true });
});

test('successful review with unavailable detail refresh retains a retryable action', async ({
  page,
  request,
}) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=review-read-lost');
  await page.goto('/review');
  await page.getByLabel('Reviewer identity').fill('alex');
  await page.getByRole('button', { name: 'Approve (a)' }).click();
  await page.getByRole('button', { name: 'Retry review' }).click();
  await expect(page.getByText('Resolved by alex')).toBeVisible();
  const w = await (await request.get('http://127.0.0.1:8100/__writes')).json();
  expect(w).toHaveLength(2);
  expect(w[0]).toEqual(w[1]);
});

test('proxy allows same origin but blocks another origin host and unknown endpoints', async ({
  page,
  request,
}) => {
  await page.goto('/review');
  await expect(page.getByRole('heading', { name: 'case-001', exact: true })).toBeVisible();
  const response = await request.post('/api/reckoner/reviews', {
    headers: { Origin: 'http://other.invalid:3100' },
    data: {
      tenant_id: 'tenant-a',
      case_id: 'case-001',
      reviewer_id: 'alex',
      verdict: 'approve',
      expected_version: 1,
      idempotency_key: 'foreign',
    },
  });
  expect(response.status()).toBe(403);
  expect(await (await request.get('http://127.0.0.1:8100/__writes')).json()).toHaveLength(0);
  expect((await request.get('/api/reckoner/provider-secrets')).status()).toBe(404);
});
test('crowded graph bounds drawing and keeps safe accessible identities', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=crowded-graph');
  await page.goto('/review');
  await expect(page.locator('svg rect')).toHaveCount(24);
  await expect(page.getByText('Device 99', { exact: true })).toBeVisible();
  expect(await page.locator('svg line').count()).toBeLessThanOrEqual(200);
  await expect(page.getByText(/Truncated graph/)).toBeVisible();
});
test('tenant and status selection stay scoped; d from queue declines once', async ({ page, request }) => {
  await page.goto('/review');
  await page.getByLabel('Tenant').selectOption('tenant-b');
  await page.getByLabel('Case status').selectOption('open');
  await expect(page.getByRole('heading', { name: 'case-001', exact: true })).toBeVisible();
  await page.getByLabel('Reviewer identity').fill('sam');
  await page.getByLabel('Case status').focus();
  await page.keyboard.press('d');
  expect(await (await request.get('http://127.0.0.1:8100/__writes')).json()).toHaveLength(0);
  await page.getByLabel('Case status').selectOption('open');
  await expect(page.getByRole('heading', { name: 'case-001', exact: true })).toBeVisible();
  await page.getByRole('button', { name: /case-001/ }).click();
  await page.keyboard.press('d');
  await expect(page.getByText('Resolved by sam')).toBeVisible();
  const w = await (await request.get('http://127.0.0.1:8100/__writes')).json();
  expect(w[0]).toMatchObject({ tenant_id: 'tenant-b', verdict: 'decline', reviewer_id: 'sam' });
});

test('minor-unit strings retain exact cents above the numeric precision boundary', async ({
  page,
  request,
}) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=large-amount');
  await page.goto('/review');
  await expect(page.getByRole('button', { name: /case-001/ })).toContainText('USD 90071992547409.93');
});
test('keyboard-selected crowded queue row stays in the queue viewport', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=crowded');
  await page.goto('/review');
  await expect(page.getByRole('heading', { name: 'case-001', exact: true })).toBeVisible();
  await page.getByRole('button', { name: /case-001/ }).click();
  for (let i = 0; i < 20; i++) await page.keyboard.press('j');
  await expect(page.getByRole('heading', { name: 'case-021', exact: true })).toBeVisible();
  await expect
    .poll(() =>
      page.getByRole('button', { name: /case-021/ }).evaluate((el) => {
        const row = el.getBoundingClientRect(),
          queue = el.parentElement!.getBoundingClientRect();
        return row.top >= queue.top && row.bottom <= queue.bottom;
      }),
    )
    .toBe(true);
});

test('focused queue row supports navigation and one decision while modifiers and repeat are ignored', async ({
  page,
  request,
}) => {
  await page.goto('/review');
  await page.getByLabel('Reviewer identity').fill('alex');
  const row = page.getByRole('button', { name: /case-001/ });
  await row.focus();
  await expect(row).toBeFocused();
  await page.keyboard.press('j');
  await expect(page.getByRole('heading', { name: 'case-002', exact: true })).toBeVisible();
  for (const shortcut of ['Alt+a', 'Control+d', 'Meta+a', 'Shift+d']) await page.keyboard.press(shortcut);
  await row.evaluate((el) =>
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', repeat: true, bubbles: true })),
  );
  expect(await (await request.get('http://127.0.0.1:8100/__writes')).json()).toHaveLength(0);
  await page.keyboard.press('d');
  await page.keyboard.press('d');
  await expect(page.getByText('Resolved by alex')).toBeVisible();
  const writes = await (await request.get('http://127.0.0.1:8100/__writes')).json();
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ case_id: 'case-002', reviewer_id: 'alex', verdict: 'decline' });
});

test('textarea and nested contenteditable retain ordinary typing without queue actions', async ({
  page,
  request,
}) => {
  await page.goto('/review');
  await page.getByLabel('Reviewer identity').fill('alex');
  await page.evaluate(() => {
    const textarea = document.createElement('textarea');
    textarea.setAttribute('aria-label', 'Temporary editing field');
    const editable = document.createElement('div');
    editable.contentEditable = 'true';
    editable.setAttribute('aria-label', 'Temporary rich editing field');
    editable.innerHTML = '<span>Editable content</span>';
    document.querySelector('main')!.append(textarea, editable);
  });
  await page.getByLabel('Temporary editing field').focus();
  await page.keyboard.press('j');
  await page.keyboard.press('a');
  await page.getByLabel('Temporary rich editing field').locator('span').click();
  await page.keyboard.press('k');
  await page.keyboard.press('d');
  await expect(page.getByRole('heading', { name: 'case-001', exact: true })).toBeVisible();
  expect(await (await request.get('http://127.0.0.1:8100/__writes')).json()).toHaveLength(0);
});
