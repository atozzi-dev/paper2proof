import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'
import { verdictSummaryText, warningTitle } from '../src/run/verdictCopy'
import type { RunEvent, RunView } from '../src/types/api'
import { claimAbs, ev, fullRun, notReproduced, runConditions } from './fixtures'

const RUN_ID = 'r_' + 'a'.repeat(26)

function sse(events: RunEvent[]): string {
  return events.map((e) => `id: ${e.id}\nevent: ${e.type}\ndata: ${JSON.stringify(e.payload)}\n\n`).join('')
}

function runView(over: Partial<RunView> = {}): RunView {
  return {
    id: RUN_ID,
    arxiv_id: '2106.09685',
    status: 'RUNNING',
    error_code: null,
    created_at: '2026-10-16T12:00:00Z',
    finished_at: null,
    listed: false,
    approval: null,
    steps: [],
    claims: [],
    verdicts: [],
    conditions: null,
    ...over,
  }
}

type Handler = (url: string, init?: RequestInit) => Response | undefined

function mockFetch(handler: Handler) {
  const fn = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const res = handler(url, init)
    return Promise.resolve(res ?? new Response('{"error":"not found"}', { status: 404 }))
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

afterEach(() => {
  vi.unstubAllGlobals()
  window.location.hash = ''
})

describe('Home', () => {
  it('rejects anything but an arXiv ID without calling the API', async () => {
    const fetchMock = mockFetch((url) => (url === '/api/gallery' ? json([]) : undefined))
    window.location.hash = '#/'
    render(<App />)
    fireEvent.change(screen.getByLabelText('arXiv ID'), { target: { value: 'https://arxiv.org/abs/2106.09685' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start the run' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/arXiv ID such as/)
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toEqual(['/api/gallery'])
  })

  it('logs in with the invite code, starts the run and opens its page', async () => {
    const posted: string[] = []
    mockFetch((url, init) => {
      if (url === '/api/gallery') return json([])
      if (init?.method === 'POST') {
        posted.push(`${url} ${init.body}`)
        if (url === '/api/session') return new Response(null, { status: 204 })
        if (url === '/api/runs') return json({ run_id: RUN_ID, events_url: `/api/runs/${RUN_ID}/events` }, 202)
      }
      if (url === `/api/runs/${RUN_ID}`) return json(runView())
      if (url.startsWith(`/api/runs/${RUN_ID}/events`)) return new Response(sse([]), { status: 200 })
      return undefined
    })
    window.location.hash = '#/'
    render(<App />)
    fireEvent.change(screen.getByLabelText('arXiv ID'), { target: { value: '2106.09685v2' } })
    fireEvent.change(screen.getByLabelText('Invite code'), { target: { value: 'invite-123' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start the run' }))
    await waitFor(() => expect(window.location.hash).toBe(`#/runs/${RUN_ID}`))
    expect(posted).toEqual([
      '/api/session {"invite_code":"invite-123"}',
      '/api/runs {"arxiv_id":"2106.09685v2","claim_hint":null}',
    ])
  })

  it('keeps the claim hint behind a disclosure and still sends it', async () => {
    const posted: string[] = []
    mockFetch((url, init) => {
      if (url === '/api/gallery') return json([])
      if (init?.method === 'POST' && url === '/api/runs') {
        posted.push(String(init.body))
        return json({ run_id: RUN_ID, events_url: `/api/runs/${RUN_ID}/events` }, 202)
      }
      if (url === `/api/runs/${RUN_ID}`) return json(runView())
      if (url.startsWith(`/api/runs/${RUN_ID}/events`)) return new Response(sse([]), { status: 200 })
      return undefined
    })
    window.location.hash = '#/'
    render(<App />)
    const hint = screen.getByLabelText('Claim to focus on (optional)')
    expect(hint.closest('details')).not.toBeNull()
    fireEvent.change(screen.getByLabelText('arXiv ID'), { target: { value: '2106.09685' } })
    fireEvent.change(hint, { target: { value: 'the LoRA rank ablation' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start the run' }))
    await waitFor(() => expect(posted).toEqual(['{"arxiv_id":"2106.09685","claim_hint":"the LoRA rank ablation"}']))
  })
})

describe('Home onboarding', () => {
  const card = {
    id: RUN_ID,
    arxiv_id: '0000.00001',
    created_at: '2026-10-16T12:00:00Z',
    status: 'COMPLETED' as const,
    verdict_summary: '1 REPRODUCED',
  }

  it('says in three steps what a run does', async () => {
    mockFetch((url) => (url === '/api/gallery' ? json([]) : undefined))
    window.location.hash = '#/'
    render(<App />)
    const steps = screen.getByRole('list', { name: 'How it works' })
    expect(within(steps).getAllByRole('listitem')).toHaveLength(3)
    expect(steps).toHaveTextContent('Reader · Nemotron 3.5 Super VL')
    expect(steps).not.toHaveTextContent(/Super(?! VL)|Nano/) // retired on 2026-11-09
  })

  it('shows the newest gallery report under the form, verdict first, with its chart and conditions', async () => {
    mockFetch((url) => {
      if (url === '/api/gallery') return json([card])
      if (url === `/api/runs/${RUN_ID}`)
        return json(runView({ status: 'COMPLETED', title: 'Deep Residual Learning', claims: [{ claim: claimAbs, selected: true }], verdicts: [{ claim_id: claimAbs.id, verdict: notReproduced }] }))
      return undefined
    })
    window.location.hash = '#/'
    render(<App />)
    const latest = await screen.findByRole('region', { name: 'Latest report' })
    expect(within(latest).getByRole('heading', { level: 2 })).toHaveTextContent('Deep Residual Learning')
    expect(within(latest).getByRole('heading', { name: 'Not reproduced under our conditions' })).toBeInTheDocument()
    expect(within(latest).getByRole('img')).toHaveAccessibleName(/tolerance/)
    expect(within(latest).getByRole('list', { name: 'Conditions' })).toHaveTextContent('Seeds')
    expect(within(latest).getByText('94.1%').tagName).toBe('MARK') // the paper's value, highlighted
    expect(within(latest).getByRole('link', { name: /Read the full report/ })).toHaveAttribute('href', `#/runs/${RUN_ID}`)
  })

  it('never puts a replay button above the form: replays start from a run card', async () => {
    mockFetch((url) => (url === '/api/gallery' ? json([card]) : undefined))
    window.location.hash = '#/'
    render(<App />)
    expect(await screen.findByRole('link', { name: 'Replay the run on arXiv 0000.00001' })).toHaveAttribute(
      'href',
      `#/runs/${RUN_ID}?replay=1`,
    )
    expect(screen.queryByRole('link', { name: /Watch a finished run/ })).toBeNull()
  })
})

describe('Gallery', () => {
  const card = (n: number, verdict_summary: string) => ({
    id: 'r_' + String(n).repeat(26),
    arxiv_id: `0000.0000${n}`,
    created_at: '2026-11-13T20:00:00Z',
    status: 'COMPLETED' as const,
    verdict_summary,
  })
  const cards = [
    card(1, '1 REPRODUCED'),
    { ...card(2, '1 REPRODUCED · 1 NOT_REPRODUCED'), title: 'Do Kernel Machines Need Feature Scaling?' },
    card(3, '1 INCONCLUSIVE'),
  ]

  it('filters the reports by verdict and by arXiv ID', async () => {
    mockFetch((url) => (url === '/api/gallery' ? json(cards) : undefined))
    window.location.hash = '#/gallery'
    render(<App />)
    const filters = await screen.findByRole('group', { name: 'Show reports' })
    const shown = () => screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)
    expect(within(filters).getAllByRole('button').map((b) => b.textContent)).toEqual([
      'All 3 papers',
      'Not reproduced under our conditions · 1',
      'Inconclusive · 1',
      'Reduced scale · 0',
      'Reproduced · 2',
    ])
    expect(within(filters).getByRole('button', { name: 'All 3 papers' })).toHaveAttribute('aria-pressed', 'true')
    expect(shown()).toHaveLength(3)

    fireEvent.click(within(filters).getByRole('button', { name: 'Not reproduced under our conditions · 1' }))
    expect(within(filters).getByRole('button', { name: 'Not reproduced under our conditions · 1' })).toHaveAttribute('aria-pressed', 'true')
    expect(shown()).toEqual(['Do Kernel Machines Need Feature Scaling?'])

    fireEvent.click(within(filters).getByRole('button', { name: 'All 3 papers' }))
    fireEvent.change(screen.getByRole('searchbox', { name: 'Find a paper' }), { target: { value: '00003' } })
    expect(shown()).toEqual(['arXiv 0000.00003'])

    fireEvent.change(screen.getByRole('searchbox', { name: 'Find a paper' }), { target: { value: 'kernel scaling' } })
    expect(shown()).toEqual(['Do Kernel Machines Need Feature Scaling?']) // words of the title, in any order

    fireEvent.change(screen.getByRole('searchbox', { name: 'Find a paper' }), { target: { value: '9999' } })
    expect(screen.getByText('No report matches.')).toBeInTheDocument()
  })
})

describe('About', () => {
  it('names the models the roles really use', async () => {
    mockFetch((url) => (url === '/api/stats' ? json({ runs_by_verdict: {} }) : undefined))
    window.location.hash = '#/about'
    render(<App />)
    const roles = await screen.findByRole('table', { name: 'Model roles' })
    for (const role of [/Reader/, /Vision/, /Coder/]) {
      expect(within(roles).getByRole('row', { name: role })).toHaveTextContent('Nemotron 3.5 Super VL')
    }
    expect(within(roles).getByRole('row', { name: /Utility/ })).toHaveTextContent('Nemotron 3.5 Lightning')
    expect(roles).not.toHaveTextContent(/Super(?! VL)|Nano/) // retired on 2026-11-09, and no Nano Omni
    expect(within(roles).getByRole('row', { name: /Coder/ })).not.toHaveTextContent('Sandboxes')
  })

  it('states the Set A results with their date, and the rule as the engine applies it', async () => {
    mockFetch((url) => (url === '/api/stats' ? json({ runs_by_verdict: { REPRODUCED: 3, NOT_REPRODUCED: 2 } }) : undefined))
    window.location.hash = '#/about'
    render(<App />)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('How we check a paper')
    const results = screen.getByRole('region', { name: 'How well it works' })
    expect(results).toHaveTextContent('16/16')
    expect(results).toHaveTextContent('0 false accusations')
    expect(results).toHaveTextContent('30 Oct 2026')
    const rule = screen.getByRole('region', { name: 'How the rule decides' })
    expect(rule).toHaveTextContent('within the tolerance of the paper’s value, or better')
    expect(rule).toHaveTextContent('can confirm a claimed gain, never accuse')
    expect(rule).not.toHaveTextContent('whole interval lies inside') // not the rule (verdict-engine.md, ABSOLUTE)
    const stats = await screen.findByRole('table', { name: 'Verdicts across all runs' })
    expect(within(stats).getByRole('row', { name: /Not reproduced under our conditions/ })).toHaveTextContent('2')
  })

  it('credits the bundled typefaces and links their licenses', async () => {
    mockFetch((url) => (url === '/api/stats' ? json({ runs_by_verdict: {} }) : undefined))
    window.location.hash = '#/about'
    render(<App />)
    const link = await screen.findByRole('link', { name: 'SIL Open Font License 1.1' })
    expect(link).toHaveAttribute('href', '/fonts/LICENSES.txt')
    expect(link.closest('p')).toHaveTextContent('Newsreader, Instrument Sans, Caveat and IBM Plex Mono')
  })
})

describe('Run page', () => {
  it('streams a run to its verdicts with the mandated copy', async () => {
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView({ status: 'COMPLETED', listed: true }))
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(fullRun()), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Not reproduced under our conditions' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Reproduced' })).toBeInTheDocument()
    const tab = (name: string) => fireEvent.click(screen.getByRole('tab', { name }))
    tab('A · Claims')
    expect(screen.getByRole('table', { name: 'Claims extracted from the paper' })).toHaveTextContent('Table 2')
    tab('F · Logs')
    expect(screen.getByText('<b>epoch 1</b>')).toBeInTheDocument()
    tab('C · Steps')
    expect(screen.getByRole('list', { name: 'Cloud jobs' })).toHaveTextContent('Experiment (exec job)')
    tab('G · Proof Bundle')
    expect(screen.getByRole('link', { name: 'Download the Proof Bundle' })).toHaveAttribute(
      'href',
      `/api/runs/${RUN_ID}/bundle`,
    )
    tab('E · Cost')
    expect(screen.getByRole('table', { name: 'Cost receipt' })).toHaveTextContent('Total$0.41')
  })

  it("heads the report with the paper's title, the arXiv ID above it", async () => {
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView({ status: 'COMPLETED', title: 'LoRA: Low-Rank Adaptation' }))
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(fullRun()), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    expect(await screen.findByRole('heading', { level: 1, name: 'LoRA: Low-Rank Adaptation' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '2106.09685' })).toHaveAttribute('href', 'https://arxiv.org/abs/2106.09685')
  })

  it('says in short what the verdicts were, the most cautionary first', async () => {
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView({ status: 'COMPLETED', listed: true }))
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(fullRun()), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    const short = await screen.findByRole('region', { name: 'In short' })
    await waitFor(() =>
      expect(short).toHaveTextContent(
        'Of the 2 claims tested, 1 was not reproduced under our conditions and 1 was reproduced.',
      ),
    )
    expect(within(short).getByRole('list', { name: 'Conditions of this run' })).toHaveTextContent('full scale')
    const verdicts = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)
    expect(verdicts.slice(0, 2)).toEqual(['Not reproduced under our conditions', 'Reproduced'])
    // each verdict sits under its claim's own h2, and the appendix parts under the Appendix h2
    expect(screen.getByRole('heading', { level: 2, name: 'Claim c1' })).toBeInTheDocument()
    expect(within(screen.getByRole('tabpanel')).queryAllByRole('heading', { level: 2 })).toHaveLength(0)
  })

  it('lists the setup the run used against the paper', async () => {
    const setup = [
      { id: 's1', kind: 'NUMBER' as const, name: 'Test split fraction', paper: '0.3', planned: '0.3', used: '0.3', matches_paper: true, where: 'workspace/train.py' },
      { id: 's2', kind: 'CHOICE' as const, name: 'Kernel', paper: 'rbf', planned: 'rbf', used: 'linear', matches_paper: false, where: '' },
    ]
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView({ status: 'COMPLETED', conditions: { ...runConditions, setup } }))
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(fullRun()), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    expect(await screen.findByRole('tab', { name: 'B · The paper’s setup' })).toHaveAttribute('aria-selected', 'true')
    const table = await screen.findByRole('table', { name: 'The paper’s setup' })
    expect(within(table).getByRole('row', { name: /Test split fraction/ })).toHaveTextContent('found in workspace/train.py')
    expect(within(table).getByRole('row', { name: /Kernel/ })).toHaveTextContent(/rbf.*linear.*differs.*declared by the Coder/)
  })

  it('moves between appendix tabs with the arrow keys', async () => {
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView({ status: 'COMPLETED' }))
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(fullRun()), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    const setupTab = await screen.findByRole('tab', { name: 'B · The paper’s setup' })
    expect(setupTab).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('tab', { name: 'C · Steps' })).toHaveAttribute('tabindex', '-1')
    fireEvent.keyDown(setupTab, { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'C · Steps' })).toHaveAttribute('aria-selected', 'true')
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'C · Steps' }))
    expect(screen.getByRole('tabpanel', { name: 'C · Steps' })).toBeInTheDocument()
    fireEvent.keyDown(document.activeElement as Element, { key: 'End' })
    expect(screen.getByRole('tab', { name: 'G · Proof Bundle' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(document.activeElement as Element, { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'A · Claims' })).toHaveAttribute('aria-selected', 'true')
  })

  it('opens the Proof Bundle part from the button in the header', async () => {
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView({ status: 'COMPLETED' }))
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(fullRun()), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Proof Bundle' }))
    expect(screen.getByRole('tab', { name: 'G · Proof Bundle' })).toHaveAttribute('aria-selected', 'true')
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'G · Proof Bundle' })))
  })

  it('shows the progress and the spend while the run is going', async () => {
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView())
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(fullRun().slice(0, 8)), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Step 3 of 10' })).toBeInTheDocument()
    const spend = screen.getByRole('region', { name: 'Spent so far' })
    await waitFor(() => expect(spend).toHaveTextContent('$0.01'))
    expect(spend).toHaveTextContent('1 model call')
    expect(screen.queryByRole('region', { name: 'In short' })).toBeNull()
  })

  it('shows the conditions the server recorded next to the verdict, as soon as it is out', async () => {
    let loads = 0
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) {
        loads += 1 // the run is still building its bundle: only the verdict is out
        return json(runView({ conditions: loads === 1 ? null : runConditions }))
      }
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(fullRun().slice(0, 18)), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    const card = (await screen.findByRole('heading', { name: 'Not reproduced under our conditions' })).closest('article')
    expect(card).not.toBeNull()
    await waitFor(() =>
      expect(within(card as HTMLElement).getByRole('list', { name: 'Conditions' })).toHaveTextContent(
        'Nebius AI Cloud gpu-l40s-a, preset 1gpu-8vcpu-32gb (preemptible)',
      ),
    )
    expect(within(card as HTMLElement).getByRole('list', { name: 'Conditions' })).toHaveTextContent(
      '10% of the data',
    )
  })

  it('warns next to the download button that the bundle is unsigned', async () => {
    const events = [...fullRun(), ev('warning', { code: 'BUNDLE_UNSIGNED', message: 'the Proof Bundle is not signed' })]
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView({ status: 'COMPLETED' }))
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(events), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    fireEvent.click(await screen.findByRole('tab', { name: 'G · Proof Bundle' }))
    const panel = screen.getByRole('tabpanel', { name: 'G · Proof Bundle' })
    await waitFor(() => expect(within(panel).getByRole('note')).toHaveTextContent('This Proof Bundle is not signed'))
    expect(within(panel).getByRole('link', { name: 'Download the Proof Bundle' })).toBeInTheDocument()
  })

  it('says in plain words that no GPU was available', async () => {
    const events = [
      ...fullRun(),
      ev('warning', { code: 'NO_GPU_CAPACITY', message: 'no GPU capacity on gpu-h100-sxm: retrying in 2 minutes' }),
    ]
    mockFetch((url) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView({ status: 'RUNNING' }))
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(events), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    const notices = (await screen.findByRole('heading', { name: 'Notices' })).closest('section') as HTMLElement
    await waitFor(() =>
      expect(notices).toHaveTextContent('No capacity for the experiment: no GPU capacity on gpu-h100-sxm: retrying in 2 minutes'),
    )
    expect(notices).not.toHaveTextContent('NO_GPU_CAPACITY')
  })

  it('lets the owner approve a pending plan', async () => {
    const approvals: string[] = []
    mockFetch((url, init) => {
      if (url === `/api/runs/${RUN_ID}`) return json(runView())
      if (url === `/api/runs/${RUN_ID}/events`) return new Response(sse(fullRun().slice(0, 11)), { status: 200 })
      if (url === `/api/runs/${RUN_ID}/approve` && init?.method === 'POST') {
        approvals.push(url)
        return new Response(null, { status: 204 })
      }
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    const approve = await screen.findByRole('button', { name: 'Approve' })
    expect(approve.closest('[role="tabpanel"]')).toBeNull() // never hidden behind a tab
    fireEvent.click(screen.getByRole('tab', { name: 'D · Plan' }))
    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(1) // one decision, one place
    fireEvent.click(approve)
    expect(await screen.findByText('Plan approved.')).toBeInTheDocument()
    expect(approvals).toEqual([`/api/runs/${RUN_ID}/approve`])
  })

  it('replays read-only: no approval buttons', async () => {
    const urls: string[] = []
    mockFetch((url) => {
      urls.push(url)
      if (url === `/api/runs/${RUN_ID}`) return json(runView({ status: 'COMPLETED', listed: true }))
      if (url.startsWith(`/api/runs/${RUN_ID}/events`)) return new Response(sse(fullRun().slice(0, 11)), { status: 200 })
      return undefined
    })
    window.location.hash = `#/runs/${RUN_ID}?replay=1`
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Plan' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    // the replay has not reached a verdict yet: nothing from the finished run shows early
    expect(screen.getByRole('heading', { name: /^Step \d+ of 10$/ })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'In short' })).toBeNull()
    expect(urls).toContain(`/api/runs/${RUN_ID}/events?replay=1&speed=10`)
  })

  it('says a private run of another session is not found', async () => {
    mockFetch(() => undefined)
    window.location.hash = `#/runs/${RUN_ID}`
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Run not found' })).toBeInTheDocument()
  })
})

describe('warningTitle', () => {
  it('names the capacity warning in plain words', () => {
    expect(warningTitle('NO_GPU_CAPACITY')).toBe('No capacity for the experiment')
  })

  it('names a repository skipped for its size', () => {
    expect(warningTitle('REPO_TOO_LARGE')).toBe('Repository too large to fetch')
  })

  it('shows a code it does not know as it came', () => {
    expect(warningTitle('SOMETHING_NEW')).toBe('SOMETHING_NEW')
    expect(warningTitle('toString')).toBe('toString')
  })
})

describe('verdictSummaryText', () => {
  it('never shows a raw NOT_REPRODUCED', () => {
    expect(verdictSummaryText('1 REPRODUCED · 1 NOT_REPRODUCED')).toBe(
      '1 Reproduced · 1 Not reproduced under our conditions',
    )
  })
})
