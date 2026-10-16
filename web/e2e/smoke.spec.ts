import { expect, test, type Route } from '@playwright/test'
import { fullRun } from '../tests/fixtures'

const RUN_ID = 'r_' + '0123456789abcdefghijklmnop'

const card = {
  id: RUN_ID,
  arxiv_id: '2106.09685',
  status: 'COMPLETED',
  verdict_summary: '1 REPRODUCED · 1 NOT_REPRODUCED',
  created_at: '2026-10-16T12:00:00Z',
}

const runView = {
  ...card,
  error_code: null,
  finished_at: '2026-10-16T12:30:00Z',
  listed: true,
  approval: 'approved',
  steps: [],
  claims: [],
  verdicts: [],
}

const sse = fullRun()
  .map((e) => `id: ${e.id}\nevent: ${e.type}\ndata: ${JSON.stringify(e.payload)}\n\n`)
  .join('')

test('gallery replay of a fixture run reaches the verdict in under 30 s', async ({ page }) => {
  const unmocked: string[] = []
  // Mocked backend: every /api call is answered here; anything else off-origin is blocked.
  await page.route(
    (url) => url.origin !== 'http://127.0.0.1:4173',
    (route: Route) => {
      unmocked.push(route.request().url())
      return route.abort()
    },
  )
  await page.route('**/api/**', (route: Route) => {
    const { pathname, search } = new URL(route.request().url())
    if (pathname === '/api/gallery') return route.fulfill({ json: [card] })
    if (pathname === `/api/runs/${RUN_ID}`) return route.fulfill({ json: runView })
    if (pathname === `/api/runs/${RUN_ID}/events` && search === '?replay=1&speed=10') {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse })
    }
    unmocked.push(route.request().url())
    return route.fulfill({ status: 404, json: { error: 'not found' } })
  })

  const violations: string[] = []
  page.on('console', (msg) => {
    if (msg.text().includes('Content Security Policy')) violations.push(msg.text())
  })
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => console.error(`Content Security Policy: ${e.violatedDirective}`))
  })

  const started = Date.now()
  const response = await page.goto('/#/gallery')
  expect(response?.headers()['content-security-policy']).toContain("default-src 'self'")
  await expect(page.getByText('1 Reproduced · 1 Not reproduced under our conditions')).toBeVisible()
  await page.getByRole('link', { name: 'Replay the run on arXiv 2106.09685' }).click()

  await expect(page.getByRole('heading', { name: 'Not reproduced under our conditions' })).toBeVisible({
    timeout: 30_000,
  })
  expect(Date.now() - started).toBeLessThan(30_000)
  await expect(page.getByRole('heading', { name: 'Reproduced', exact: true })).toBeVisible()
  await expect(page.getByRole('status').filter({ hasText: 'Up to date' })).toBeVisible()
  await expect(page.getByText('Completed', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Approve' })).toHaveCount(0)
  expect(unmocked).toEqual([])
  expect(violations).toEqual([])
})
