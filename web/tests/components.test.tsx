import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CostReceiptView } from '../src/components/CostReceiptView'
import { JobsList } from '../src/components/JobsList'
import { LogPanel } from '../src/components/LogPanel'
import { SafeMarkdown } from '../src/components/SafeMarkdown'
import { VerdictCard } from '../src/components/VerdictCard'
import type { Verdict } from '../src/types/api'
import { claimAbs, claimRel, notReproduced, plan, reproduced, runConditions } from './fixtures'

const conditions = {
  plan: { plan, est_cost_usd: 1, needs_approval: false },
  warnings: [{ code: 'JOB_PREEMPTED', message: 'The GPU job was preempted: retried once on demand.' }],
  jobs: { 'p2p-exec-abc-1': { kind: 'exec' as const, name: 'p2p-exec-abc-1', state: 'SUCCEEDED' } },
}

describe('VerdictCard', () => {
  it('shows NOT_REPRODUCED as "Not reproduced under our conditions" with the conditions next to it', () => {
    render(<VerdictCard claim={claimAbs} verdict={notReproduced} explanationMd="" {...conditions} />)
    const card = screen.getByRole('article')
    expect(within(card).getByRole('heading', { name: 'Not reproduced under our conditions' })).toBeInTheDocument()
    expect(card).not.toHaveTextContent(/NOT_REPRODUCED/)
    const list = within(card).getByRole('list', { name: 'Conditions' })
    expect(list).toHaveTextContent('Full scale')
    expect(list).toHaveTextContent('3 seeds')
    expect(list).toHaveTextContent('p2p-exec-abc-1')
    expect(list).toHaveTextContent('Learning-rate schedule not specified in the paper: cosine used.')
    expect(list).toHaveTextContent('The GPU job was preempted')
  })

  it('shows reduced-scale reproduction with its scale', () => {
    const v: Verdict = { ...reproduced, kind: 'REPRODUCED_AT_REDUCED_SCALE' }
    const reduced = { ...conditions, plan: { ...conditions.plan, plan: { ...plan, scale: 'REDUCED' as const } } }
    render(<VerdictCard claim={claimRel} verdict={v} explanationMd="" {...reduced} />)
    expect(screen.getByRole('heading', { name: 'Reproduced at reduced scale' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Conditions' })).toHaveTextContent('Reduced scale')
  })

  it('lists the recorded conditions: scale factor, seeds used and the hardware', () => {
    render(
      <VerdictCard claim={claimAbs} verdict={notReproduced} explanationMd="" {...conditions} conditions={runConditions} />,
    )
    const card = screen.getByRole('article')
    expect(within(card).getByRole('heading', { name: 'Not reproduced under our conditions' })).toBeInTheDocument()
    const list = within(card).getByRole('list', { name: 'Conditions' })
    expect(list).toHaveTextContent("Scale: Reduced scale: 10% of the data, 5 of the paper's 200 epochs")
    expect(list).toHaveTextContent('Seeds: 3 seeds (seeds 0, 1 of the planned 0, 1, 2)')
    expect(list).toHaveTextContent(
      'Hardware: Nebius AI Cloud gpu-l40s-a, preset 1gpu-8vcpu-32gb (preemptible) · GPU reported by the job: NVIDIA L40S',
    )
    expect(list).toHaveTextContent('Code: Written from the paper')
    expect(list).toHaveTextContent('Smaller model than the paper.')
    expect(list).toHaveTextContent('The GPU job was preempted')
    expect(list).not.toHaveTextContent(/not reported/)
  })

  it('shows the scale factor next to a reduced-scale reproduction', () => {
    const v: Verdict = { ...reproduced, kind: 'REPRODUCED_AT_REDUCED_SCALE' }
    const onlyData = { ...runConditions, scale: { ...runConditions.scale, epochs: null, paper_epochs: null } }
    render(<VerdictCard claim={claimRel} verdict={v} explanationMd="" {...conditions} conditions={onlyData} />)
    expect(screen.getByRole('list', { name: 'Conditions' })).toHaveTextContent('Reduced scale: 10% of the data')
  })

  it('says when a reduced run recorded no scale factor and when no hardware was recorded', () => {
    const bare = {
      ...runConditions,
      scale: { scale: 'REDUCED' as const, fraction_of_data: null, epochs: null, paper_epochs: null },
      hardware: { platform: null, preset: null, preemptible: null, gpu: null },
    }
    render(
      <VerdictCard claim={claimAbs} verdict={notReproduced} explanationMd="" {...conditions} jobs={{}} conditions={bare} />,
    )
    const list = screen.getByRole('list', { name: 'Conditions' })
    expect(list).toHaveTextContent('the scale factor is not reported for this run')
    expect(list).toHaveTextContent('Hardware: Not reported for this run')
  })

  it('shows INCONCLUSIVE with a human-readable reason', () => {
    const v: Verdict = { ...notReproduced, kind: 'INCONCLUSIVE', reason: 'HIGH_VARIANCE' }
    render(<VerdictCard claim={claimAbs} verdict={v} explanationMd="" {...conditions} />)
    expect(screen.getByRole('heading', { name: 'Inconclusive' })).toBeInTheDocument()
    expect(screen.getByText(/confidence interval is too wide/i)).toBeInTheDocument()
  })

  it('draws the confidence interval against the tolerance band', () => {
    render(<VerdictCard claim={claimAbs} verdict={notReproduced} explanationMd="" {...conditions} />)
    const chart = screen.getByRole('img', { name: /95% CI/ })
    expect(chart.getAttribute('aria-label')).toMatch(/0\.907.*0\.917.*0\.9222.*0\.9598/)
  })
})

describe('SafeMarkdown', () => {
  it('renders formatting but never raw HTML, images or live links', () => {
    const { container } = render(
      <SafeMarkdown
        text={'The **mean** is low.\n\n<img src=x onerror="alert(1)"><script>alert(1)</script>\n\n[click](javascript:alert(1)) ![p](https://evil.example/p.png)'}
      />,
    )
    expect(container.querySelector('strong')).toHaveTextContent('mean')
    expect(container.querySelector('img, script, a, iframe')).toBeNull()
    expect(container.innerHTML).not.toMatch(/onerror|javascript:/)
    expect(container).toHaveTextContent('click')
  })
})

describe('LogPanel', () => {
  it('shows untrusted lines as plain text', () => {
    const lines = [{ key: 1, source: 'iter 0 · stdout', stderr: false, text: '<b>bold?</b><img src=x>' }]
    const { container } = render(<LogPanel title="Sandbox output" lines={lines} />)
    expect(container.querySelector('b, img')).toBeNull()
    expect(screen.getByText('<b>bold?</b><img src=x>')).toBeInTheDocument()
  })

  it('renders only a window of a long log', () => {
    const lines = Array.from({ length: 5000 }, (_, i) => ({ key: i, source: 'exec', stderr: false, text: `line ${i}` }))
    render(<LogPanel title="Job log" lines={lines} />)
    expect(screen.getAllByText(/^line \d+$/).length).toBeLessThan(200)
  })
})

describe('CostReceiptView', () => {
  it('lists each role and the totals', () => {
    render(
      <CostReceiptView
        calls={[
          { role: 'reader', model_id: 'm', tokens_in: 1000, tokens_out: 200, latency_ms: 1, cost_usd: 0.1 },
          { role: 'planner', model_id: 'm', tokens_in: 2000, tokens_out: 400, latency_ms: 1, cost_usd: 0.25 },
        ]}
        update={{ inference_usd: 0.35, cloud_usd: 1.5, total_usd: 1.85 }}
      />,
    )
    const table = screen.getByRole('table', { name: 'Cost receipt' })
    expect(within(table).getByRole('row', { name: /reader/ })).toHaveTextContent('$0.10')
    expect(within(table).getByRole('row', { name: /^Total/ })).toHaveTextContent('$1.85')
    expect(within(table).getByRole('row', { name: /^Cloud jobs/ })).toHaveTextContent('$1.50')
  })

  it('never shows $0.00 of cloud spend before the server reported any', () => {
    render(
      <CostReceiptView
        calls={[{ role: 'reader', model_id: 'm', tokens_in: 1, tokens_out: 1, latency_ms: 1, cost_usd: 0.1 }]}
        update={null}
      />,
    )
    const table = screen.getByRole('table', { name: 'Cost receipt' })
    expect(within(table).getByRole('row', { name: /^Cloud jobs/ })).toHaveTextContent('not reported yet')
    expect(within(table).getByRole('row', { name: /^Total/ })).toHaveTextContent('$0.10 + cloud not reported yet')
  })
})

describe('VerdictCard without the full context', () => {
  it('never claims "no deviations" before the plan is known', () => {
    render(<VerdictCard claim={claimAbs} verdict={notReproduced} explanationMd="" plan={null} warnings={[]} jobs={{}} />)
    const list = screen.getByRole('list', { name: 'Conditions' })
    expect(list).toHaveTextContent('Scale: not reported yet')
    expect(list).toHaveTextContent('Deviations from the paper: not available yet')
    expect(list).not.toHaveTextContent('None recorded')
  })

  it('still shows a verdict whose claim is not loaded', () => {
    render(<VerdictCard claimId="c9" claim={null} verdict={notReproduced} explanationMd="" {...conditions} />)
    expect(screen.getByRole('heading', { name: 'Not reproduced under our conditions' })).toBeInTheDocument()
    expect(screen.getByRole('article')).toHaveTextContent('Claim c9')
  })
})

describe('JobsList', () => {
  it('names every job kind, the fetch job included, apart from provisioning', () => {
    render(
      <JobsList
        jobs={{
          'p2p-prov-abc-1': { kind: 'prov', name: 'p2p-prov-abc-1', state: 'SUCCEEDED' },
          'p2p-fetch-abc-1': { kind: 'fetch', name: 'p2p-fetch-abc-1', state: 'RUNNING' },
          'p2p-exec-abc-1': { kind: 'exec', name: 'p2p-exec-abc-1', state: 'PENDING' },
        }}
      />,
    )
    const list = screen.getByRole('list', { name: 'Cloud jobs' })
    const items = within(list).getAllByRole('listitem')
    expect(items).toHaveLength(3)
    expect(items[0]).toHaveTextContent('Packages (provision job)')
    expect(items[1]).toHaveTextContent('Repository and datasets (fetch job)')
    expect(items[1]).toHaveTextContent('p2p-fetch-abc-1')
    expect(items[1]).toHaveTextContent('RUNNING')
    expect(items[2]).toHaveTextContent('Experiment on GPU (exec job)')
  })

  it('says when no job has started', () => {
    render(<JobsList jobs={{}} />)
    expect(screen.getByText('No cloud job yet.')).toBeInTheDocument()
  })
})
