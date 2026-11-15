import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CiBandChart } from '../src/components/CiBandChart'
import { ClaimQuote } from '../src/components/ClaimQuote'
import { VerdictPill } from '../src/components/VerdictPill'
import { splitHighlight } from '../src/run/highlight'
import { chartSpec, ticks } from '../src/run/chart'
import { fmtDuration } from '../src/run/format'
import { conditionChips, inShort, parseSummary, pillText } from '../src/run/summary'
import type { PlanReady, RunConditions } from '../src/types/api'
import { claimAbs, claimRel, notReproduced, plan, reproduced, runConditions } from './fixtures'

const marked = (parts: { text: string; mark: boolean }[]) => parts.filter((p) => p.mark).map((p) => p.text)

describe('splitHighlight', () => {
  it("marks the paper's value with its spread", () => {
    const claim = { ...claimAbs, text: 'RBF SVM reaches a test ROC AUC of 0.966 ± 0.005.', reported_value: 0.966 }
    const parts = splitHighlight(claim.text, claim)
    expect(marked(parts)).toEqual(['0.966 ± 0.005'])
    expect(parts.map((p) => p.text).join('')).toBe(claim.text)
  })

  it('marks a claimed gain written with more digits than the value', () => {
    const claim = { ...claimRel, text: 'Raw features beat the standardized SVM by 0.020 ROC AUC.', claimed_delta: 0.02 }
    expect(marked(splitHighlight(claim.text, claim))).toEqual(['0.020'])
  })

  it('never marks a number inside a name, and prefers the one with a unit', () => {
    const claim = { ...claimAbs, text: 'On CIFAR-10 the error is 10%.', reported_value: 0.1 }
    const parts = splitHighlight(claim.text, claim)
    expect(marked(parts)).toEqual(['10%'])
    const one = { ...claimRel, text: 'It beats the baseline by 1 point.', claimed_delta: 0.01 }
    expect(marked(splitHighlight(one.text, one))).toEqual(['1 point'])
  })

  it('marks a value written as a percentage or in points', () => {
    expect(marked(splitHighlight(claimAbs.text, claimAbs))).toEqual(['94.1%'])
    expect(marked(splitHighlight(claimRel.text, claimRel))).toEqual(['1.7 points'])
  })

  it('leaves the text alone when the value is not in it, or there is no value', () => {
    const text = 'Our method reaches state-of-the-art accuracy on CIFAR-10.'
    expect(splitHighlight(text, claimAbs)).toEqual([{ text, mark: false }])
    const vague = { ...claimRel, claimed_delta: null }
    expect(splitHighlight(claimRel.text, vague)).toEqual([{ text: claimRel.text, mark: false }])
  })
})

describe('parseSummary', () => {
  it('counts each kind, the most cautionary first', () => {
    expect(parseSummary('2 REPRODUCED · 1 NOT_REPRODUCED')).toEqual({
      counts: [
        { kind: 'NOT_REPRODUCED', count: 1 },
        { kind: 'REPRODUCED', count: 2 },
      ],
      more: 0,
    })
  })

  it('keeps the kinds the server folded into "+n more"', () => {
    expect(parseSummary('1 REPRODUCED · +3 more').more).toBe(3)
  })

  it('has nothing to count without a summary', () => {
    expect(parseSummary(null)).toEqual({ counts: [], more: 0 })
  })
})

describe('pillText', () => {
  it('puts the count before the mandated copy', () => {
    expect(pillText('NOT_REPRODUCED', 2)).toBe('2 not reproduced under our conditions')
    expect(pillText('NOT_REPRODUCED')).toBe('Not reproduced under our conditions')
  })
})

describe('inShort', () => {
  it('says what happened to a single claim', () => {
    expect(inShort(['NOT_REPRODUCED'])).toBe('The claim tested was not reproduced under our conditions.')
  })

  it('counts the verdicts of several claims, the most cautionary first', () => {
    expect(inShort(['REPRODUCED', 'NOT_REPRODUCED'])).toBe(
      'Of the 2 claims tested, 1 was not reproduced under our conditions and 1 was reproduced.',
    )
    expect(inShort(['REPRODUCED', 'REPRODUCED', 'INCONCLUSIVE'])).toBe(
      'Of the 3 claims tested, 1 was inconclusive and 2 were reproduced.',
    )
  })
})

describe('conditionChips', () => {
  const sandbox: RunConditions = {
    ...runConditions,
    scale: { scale: 'FULL', fraction_of_data: null, epochs: null, paper_epochs: null },
    seeds_used: [0, 1, 2],
    hardware: { platform: 'sandbox', preset: null, preemptible: null, gpu: 'cpu' },
    setup: Array.from({ length: 7 }, (_, i) => ({
      id: `s${i}`,
      kind: i < 3 ? ('NUMBER' as const) : ('CHOICE' as const),
      name: `Condition ${i}`,
      paper: '1',
      planned: '1',
      used: '1',
      matches_paper: true,
      where: 'workspace/train.py',
    })),
  }

  it('sums up a sandbox run on the paper setup', () => {
    expect(conditionChips(sandbox, null)).toEqual([
      'full scale',
      '7 of 7 setup conditions match',
      '3 seeds',
      'CPU sandbox, no network',
      'code written from the paper',
    ])
  })

  it('counts the setup conditions that differ', () => {
    const one = { ...sandbox, setup: sandbox.setup.map((s, i) => (i === 0 ? { ...s, matches_paper: false } : s)) }
    expect(conditionChips(one, null)).toContain('1 of 7 setup conditions differ')
  })

  it('falls back on the plan before the conditions are recorded', () => {
    const ready: PlanReady = { plan, est_cost_usd: 1, needs_approval: false }
    expect(conditionChips(null, ready)).toEqual(['full scale', '3 seeds', "code adapted from the authors' repository"])
  })

  it('says CPU only when the job reported no GPU', () => {
    const cpu = { ...runConditions, hardware: { ...runConditions.hardware, gpu: 'cpu' } }
    expect(conditionChips(cpu, null)).toContain('gpu-l40s-a, CPU only')
  })

  it('names a GPU run by its platform and GPU', () => {
    expect(conditionChips(runConditions, null)).toEqual([
      'reduced scale',
      '2 seeds',
      'gpu-l40s-a, NVIDIA L40S',
      'code written from the paper',
    ])
  })
})

describe('VerdictPill', () => {
  it('never shows a raw kind and draws a shape, not only a colour', () => {
    const { container } = render(<VerdictPill kind="NOT_REPRODUCED" />)
    expect(container).toHaveTextContent('Not reproduced under our conditions')
    expect(container.querySelector('svg')).not.toBeNull()
  })

  it('can be the heading of a verdict', () => {
    render(<VerdictPill kind="REPRODUCED" as="h3" />)
    expect(screen.getByRole('heading', { level: 3, name: 'Reproduced' })).toBeInTheDocument()
  })
})

describe('CiBandChart', () => {
  it("labels the paper's value, the band and our seeds", () => {
    const { container } = render(<CiBandChart claim={claimAbs} verdict={notReproduced} />)
    const svg = container.querySelector('svg') as SVGElement
    expect(svg).toHaveTextContent('paper 0.941')
    expect(svg).toHaveTextContent('≥ 0.9222 counts as reproduced') // one-sided: a higher mean counts too
    expect(svg).toHaveTextContent('us, 3 seeds')
    expect(svg).toHaveAccessibleName(/0\.907 to 0\.917.*a mean of 0\.9222 or more counts as reproduced/)
  })

  it('opens the zone downwards for an absolute claim where lower is better', () => {
    const err = { ...claimAbs, metric: 'error', higher_is_better: false, reported_value: 0.059 }
    const v = { ...notReproduced, mean: 0.088, ci_low: 0.083, ci_high: 0.093, tolerance: { w: 0.01, v: 0.059 } }
    const { container } = render(<CiBandChart claim={err} verdict={v} />)
    expect(container.querySelector('svg')).toHaveTextContent('≤ 0.069 counts as reproduced')
  })

  it('shows the claimed gain and the gain needed for a relative claim', () => {
    const { container } = render(<CiBandChart claim={claimRel} verdict={reproduced} />)
    const svg = container.querySelector('svg') as SVGElement
    expect(svg).toHaveTextContent('paper +0.017')
    expect(svg).toHaveTextContent('≥ 0.0085 needed')
  })

  it('points the needed zone down when lower is better', () => {
    const lower = { ...claimRel, higher_is_better: false, claimed_delta: -0.017 }
    const v = { ...reproduced, mean: -0.02, ci_low: -0.022, ci_high: -0.018, tolerance: { rho: 0.5, delta: -0.017 } }
    const { container } = render(<CiBandChart claim={lower} verdict={v} />)
    const svg = container.querySelector('svg') as SVGElement
    expect(svg).toHaveTextContent('≤ -0.0085 needed')
    expect(svg).toHaveAccessibleName(/a mean change of at most -0\.0085, with the whole interval below 0/)
  })

  it('never shades a gain the rule cannot accept: the whole interval must clear zero', () => {
    // a lower-is-better claim whose gain came out with the wrong sign: +0.5, so rho x delta = +0.25
    const lower = { ...claimRel, higher_is_better: false, claimed_delta: 0.5 }
    const v = { ...reproduced, kind: 'NOT_REPRODUCED' as const, mean: 0.1, ci_low: 0.08, ci_high: 0.12, tolerance: { rho: 0.5, delta: 0.5 } }
    const spec = chartSpec(lower, v)
    expect(spec?.zone?.to).toBe(0)
  })

  it('says threshold, not "counts as reproduced", when a reason overrides the comparison', () => {
    const reduced = { ...notReproduced, kind: 'INCONCLUSIVE' as const, reason: 'REDUCED_SCALE_ABSOLUTE' as const, mean: 0.95, ci_low: 0.95, ci_high: 0.95 }
    const { container } = render(<CiBandChart claim={claimAbs} verdict={reduced} />)
    const svg = container.querySelector('svg') as SVGElement
    expect(svg).not.toHaveTextContent('counts as reproduced')
    expect(svg).toHaveTextContent('≥ 0.9222 would count at full scale')
  })

  it('draws no paper value for a claim that only says "better than"', () => {
    const vague = { ...claimRel, claimed_delta: null }
    const v = { ...reproduced, tolerance: { rho: 0.5, delta: 0 } }
    const { container } = render(<CiBandChart claim={vague} verdict={v} />)
    expect(container.querySelector('svg')).not.toHaveTextContent('paper')
  })
})

describe('fmtDuration', () => {
  it('rolls 59.6 s over to a minute', () => {
    expect(fmtDuration(59_600)).toBe('1 min 0 s')
    expect(fmtDuration(52_700)).toBe('53 s')
    expect(fmtDuration(9_960)).toBe('10 s')
    expect(fmtDuration(2_600)).toBe('2.6 s')
    expect(fmtDuration(119_000)).toBe('1 min 59 s')
  })
})

describe('ticks', () => {
  it('ends even when the step is below the precision of the values', () => {
    expect(ticks(1e9, 1e9 + 1e-7).length).toBeLessThanOrEqual(50)
  })

  it('gives round values over the range', () => {
    expect(ticks(-0.04, 0.03)).toEqual([-0.04, -0.02, 0, 0.02])
  })
})

describe('ClaimQuote', () => {
  it("quotes the claim as plain text with the paper's value marked", () => {
    const claim = { ...claimAbs, text: '<b>bold</b> reaches 94.1% top-1 accuracy' }
    const { container } = render(<ClaimQuote claim={claim} />)
    expect(container.querySelector('blockquote')).toHaveTextContent('“<b>bold</b> reaches 94.1% top-1 accuracy”')
    expect(container.querySelector('mark')).toHaveTextContent('94.1%')
    expect(container.querySelector('b')).toBeNull()
  })
})
