import { test, expect } from '@playwright/test';
test.beforeEach(async ({ request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=settings');
});
test('immutable thresholds, qualified reuse, score preview and future activation', async ({
  page,
  request,
}) => {
  await page.goto('/settings');
  for (const label of [
    'Review cost (USD)',
    'Margin rate',
    'Low floor',
    'Low ceiling',
    'High threshold',
    'Amount aware',
    'Scorer model',
    'Note model',
  ])
    await expect(page.getByLabel(label, { exact: true })).toBeVisible();
  await expect(page.getByRole('option', { name: 'unsupported/fake' })).toHaveJSProperty('disabled', true);
  await page.getByLabel('Scorer model', { exact: true }).evaluate((el) => {
    const select = el as HTMLSelectElement;
    select.value = 'unsupported/fake';
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await expect(page.getByRole('button', { name: 'Save new version' })).toBeDisabled();
  await page.getByLabel('Scorer model', { exact: true }).selectOption('jev-1.13.0');
  await page.getByLabel('Review cost (USD)').fill('5.00');
  await page.getByRole('button', { name: 'Save new version' }).click();
  await expect(page.getByLabel('Selected configuration')).toHaveValue('config-2');
  const w = await (await request.get('http://127.0.0.1:8100/__writes')).json();
  expect(w[0].configuration.calibration_id).toBe('calibration-a');
  expect(w[0]).not.toHaveProperty('calibration');
  expect(w[0].configuration).not.toHaveProperty('config_id');
  expect(w[0].configuration).not.toHaveProperty('threshold_config_id');
  await page.getByLabel('Preview run ID').fill('run-a');
  await page.getByRole('button', { name: 'Preview stored scores' }).click();
  await expect(page.getByText('5 completed decisions')).toBeVisible();
  await expect(page.getByText(/Note and model costs not estimated/)).toBeVisible();
  await expect(page.getByText(/Equality at either boundary escalates/)).toBeVisible();
  await page.getByRole('button', { name: 'Activate for future runs' }).click();
  await expect(page.getByText('Current defaults: config-2 · activation version 2')).toBeVisible();
  await page.getByLabel('Selected configuration').selectOption('config-a');
  await expect(page.getByLabel('Review cost (USD)')).toHaveValue('4.00');
  await page.screenshot({ path: 'artifacts/settings-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390);
  await page.screenshot({ path: 'artifacts/settings-mobile.png', fullPage: true });
});
test('changed scorer context rejects inherited calibration', async ({ page }) => {
  await page.goto('/settings');
  await page.getByLabel('Scoring question version').fill('changed');
  await expect(page.getByText(/Changed scoring context/)).toBeVisible();
  await page.getByRole('button', { name: 'Save new version' }).click();
  await expect(page.locator('main').getByRole('alert')).toContainText('qualification');
  await expect(page.getByLabel('Selected configuration')).toHaveValue('config-a');
});
test('stale activation explicitly refreshes current defaults', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=activation-stale');
  await page.goto('/settings');
  await page.getByRole('button', { name: 'Activate for future runs' }).click();
  await expect(page.locator('main').getByRole('alert')).toContainText('conflict');
  await page.getByRole('button', { name: 'Refresh current defaults' }).click();
  await expect(page.getByText('Current defaults: config-a · activation version 3')).toBeVisible();
});
test('lost activation retries original receipt then refreshes after ABA', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=activation-lost');
  await page.goto('/settings');
  await page.getByRole('button', { name: 'Activate for future runs' }).click();
  await page.getByRole('button', { name: 'Retry activation' }).click();
  await expect(page.getByText('Current defaults: config-a · activation version 4')).toBeVisible();
  const w = await (await request.get('http://127.0.0.1:8100/__writes')).json();
  expect(w[0]).toEqual(w[1]);
});
test('incompatible preview does not imply a changed model prediction', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=preview-incompatible');
  await page.goto('/settings');
  await page.getByLabel('Preview run ID').fill('run-a');
  await page.getByRole('button', { name: 'Preview stored scores' }).click();
  await expect(page.getByText(/Incompatible scoring context/)).toBeVisible();
});

test('uninitialized and unavailable settings do not invent workload context', async ({ page, request }) => {
  await request.get('http://127.0.0.1:8100/__scenario?name=settings-empty');
  await page.goto('/settings');
  await expect(
    page.getByText('No configuration is available for this tenant.', { exact: false }),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save new version' })).toHaveCount(0);
  await request.get('http://127.0.0.1:8100/__scenario?name=reckoner-unavailable');
  await page.reload();
  await expect(page.locator('main').getByRole('alert')).toContainText('Settings unavailable');
});
