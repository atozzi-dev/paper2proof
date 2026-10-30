import { defineConfig } from '@playwright/test'

const PORT = 4173

// One smoke test against the production build. The backend is mocked in the test itself
// (page.route), so nothing reaches a real API. Uses the installed Chrome by default; set
// PLAYWRIGHT_CHANNEL=chromium after `npx playwright install chromium` to use the bundled one.
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome',
    serviceWorkers: 'block',
  },
  webServer: {
    command: `npm run build && npx vite preview --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
})
