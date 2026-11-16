import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PlanCard } from '../src/components/PlanCard'
import { VerdictCard } from '../src/components/VerdictCard'
import { claimAbs, notReproduced, plan, runConditions } from './fixtures'

// A-46: the plan lists up to ten seeds; after three, the runner starts one more only while it
// fits the time target, and the seeds it leaves out are "not started", never failures.

const ready = { plan, est_cost_usd: 1, needs_approval: false }
const conditions = { plan: ready, warnings: [], jobs: {} }
const ran = [0, 1, 2, 3, 4, 5, 6]

function seedsLine(used: number[]): string {
  const timed = {
    ...runConditions,
    seeds_planned: [...ran, 7, 8, 9],
    seeds_used: used,
    seeds_used_by_claim: { c1: used, c2: used },
    seeds_not_started: [7, 8, 9],
  }
  render(
    <VerdictCard claim={claimAbs} verdict={{ ...notReproduced, n_seeds: used.length }} explanationMd="" {...conditions} conditions={timed} />,
  )
  return screen.getByRole('list', { name: 'Conditions' }).textContent ?? ''
}

describe('seeds sized by time', () => {
  it('calls the seeds the time target left out not started, not failed', () => {
    const line = seedsLine(ran)
    expect(line).toContain('Seeds: 7 seeds (seeds 0, 1, 2, 3, 4, 5, 6; time limit reached: 3 more not started)')
    expect(line).not.toContain('planned')
  })

  it('names the seeds that ran when one of them gave no value', () => {
    expect(seedsLine([0, 1, 2, 4, 5, 6])).toContain(
      'Seeds: 6 seeds (seeds 0, 1, 2, 4, 5, 6 of the 7 that ran; time limit reached: 3 more not started)',
    )
  })

  it('shows a plan of ten seeds as a ceiling', () => {
    const ten = { ...ready, plan: { ...plan, seeds: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9] } }
    render(<PlanCard runId="r_x" plan={ten} approval={null} readOnly />)
    expect(screen.getByText('0, 1, 2, 3, 4, 5, 6, 7, 8, 9 (at least 3; the rest while time allows)')).toBeInTheDocument()
  })

  it('shows a plan of three seeds as it is', () => {
    render(<PlanCard runId="r_x" plan={ready} approval={null} readOnly />)
    expect(screen.getByText('0, 1, 2')).toBeInTheDocument()
  })
})
