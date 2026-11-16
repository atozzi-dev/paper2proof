import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PlanCard } from '../src/components/PlanCard'
import { VerdictCard } from '../src/components/VerdictCard'
import { claimAbs, notReproduced, plan, runConditions } from './fixtures'

// A-47: a plan that would be cut on CPU gets one GPU; if none starts in time, its CPU version
// runs, and the verdict's conditions say so.

const cpuPlan = { ...plan, scale: 'REDUCED' as const }
const onGpu = {
  plan: { ...plan, scale: 'FULL' as const },
  est_cost_usd: 0.9,
  needs_approval: true,
  target: 'gpu' as const,
  cpu_plan: cpuPlan,
  est_cost_max_usd: 1.7,
}

describe('GPU when needed', () => {
  it('shows where a GPU plan runs, its worst case and its CPU fallback', () => {
    render(<PlanCard runId="r_x" plan={onGpu} approval={null} readOnly />)
    expect(screen.getByText('One NVIDIA L40S GPU')).toBeInTheDocument()
    expect(screen.getByText(/at most \$1\.70/)).toBeInTheDocument()
    expect(
      screen.getByText('If no GPU becomes free in time, a CPU version of this plan runs instead, at reduced scale.'),
    ).toBeInTheDocument()
  })

  it('shows a CPU plan on CPU, with why a GPU was not used', () => {
    const skipped = 'a GPU would help this experiment, but this invite has no GPU runs left today'
    const ready = { plan: cpuPlan, est_cost_usd: 0.2, needs_approval: false, target: 'cpu' as const, gpu_skipped: skipped }
    render(<PlanCard runId="r_x" plan={ready} approval={null} readOnly />)
    expect(screen.getByText('CPU (Token Factory Sandbox)')).toBeInTheDocument()
    expect(screen.getByText('A GPU would help this experiment, but this invite has no GPU runs left today.')).toBeInTheDocument()
    expect(screen.queryByText(/If no GPU becomes free/)).toBeNull()
  })

  it('says next to the verdict why a GPU run ran on CPU', () => {
    const message = 'no GPU was free within 10 minutes: the CPU plan runs instead'
    const conditions = {
      ...runConditions,
      hardware: { platform: 'sandbox', preset: '4vcpu-4gb', preemptible: false, gpu: null },
      gpu_fallback: message,
    }
    render(
      <VerdictCard
        claim={claimAbs}
        verdict={notReproduced}
        explanationMd=""
        plan={onGpu}
        warnings={[]}
        jobs={{}}
        conditions={conditions}
      />,
    )
    const line = screen.getByRole('list', { name: 'Conditions' }).textContent ?? ''
    expect(line).toContain(`GPU: Planned for one GPU; ${message}`)
  })
})
