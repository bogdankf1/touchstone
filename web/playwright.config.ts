import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  workers: 1,
  use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:3100' },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: [
    {
      command: 'node tests/fixture-api.mjs',
      url: 'http://127.0.0.1:8100/healthz',
      reuseExistingServer: false,
    },
    {
      command: 'npm run dev -- --hostname 127.0.0.1 --port 3100',
      url: 'http://127.0.0.1:3100',
      reuseExistingServer: false,
      env: { TOUCHSTONE_API_URL: 'http://127.0.0.1:8100', RECKONER_API_URL: 'http://127.0.0.1:8100' },
    },
  ],
});
