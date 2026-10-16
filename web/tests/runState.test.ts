import { describe, expect, it } from 'vitest'
import { initialRunState, reduceRun, type RunState } from '../src/run/state'
import { costReceipt } from '../src/run/cost'
import { ev, fullRun } from './fixtures'

function replay(events = fullRun()): RunState {
  return events.reduce(reduceRun, initialRunState())
}

describe('reduceRun', () => {
  it('builds the timeline, claims, plan, logs and verdicts from the events', () => {
    const s = replay()
    expect(s.status).toBe('COMPLETED')
    expect(s.steps.map((x) => [x.step, x.state])).toEqual([
      ['INGESTING', 'ok'],
      ['EXTRACTING_CLAIMS', 'ok'],
      ['AWAITING_APPROVAL', 'ok'],
    ])
    expect(s.claims.map((c) => c.id)).toEqual(['c1', 'c2'])
    expect(s.plan?.est_cost_usd).toBe(1.25)
    expect(s.approval).toEqual({ expires_at: '2026-10-16T13:00:00Z', pending: false })
    expect(s.sandboxLines.map((l) => l.text)).toEqual(['<b>epoch 1</b>', 'ok'])
    expect(s.jobLines.map((l) => l.text)).toEqual(['seed 0: 0.912'])
    expect(s.jobs['p2p-exec-abc-1'].state).toBe('RUNNING')
    expect(Object.keys(s.verdicts)).toEqual(['c1', 'c2'])
    expect(s.warnings.map((w) => w.code)).toEqual(['JOB_PREEMPTED'])
    expect(s.lastEventId).toBe(20)
  })

  it('keeps approval pending until the approval step finishes', () => {
    const events = fullRun().slice(0, 11)
    expect(replay(events).approval?.pending).toBe(true)
  })

  it('ignores events it has already applied', () => {
    const events = fullRun()
    const once = replay(events)
    const twice = events.reduce(reduceRun, once)
    expect(twice).toBe(once)
  })

  it('marks a failed step and records run errors', () => {
    const s = replay([
      ev('step.started', { step: 'SMOKE_TESTING', attempt: 1 }),
      ev('step.finished', {
        step: 'SMOKE_TESTING',
        attempt: 1,
        ok: false,
        error_code: 'SMOKE_EXHAUSTED',
        duration_ms: 10,
      }),
      ev('error', { code: 'SMOKE_EXHAUSTED', message: 'No branch passed the smoke test.' }),
      ev('run.status', { status: 'FAILED', error_code: 'SMOKE_EXHAUSTED' }),
    ])
    expect(s.steps[0]).toMatchObject({ step: 'SMOKE_TESTING', state: 'failed', error_code: 'SMOKE_EXHAUSTED' })
    expect(s.errors).toHaveLength(1)
    expect(s.errorCode).toBe('SMOKE_EXHAUSTED')
  })
})

describe('costReceipt', () => {
  it('sums tokens and dollars per role and adds the cloud spend', () => {
    const call = (role: string, tokens_in: number, tokens_out: number, cost_usd: number | null) => ({
      role,
      model_id: `${role}-model`,
      tokens_in,
      tokens_out,
      latency_ms: 1,
      cost_usd,
    })
    const receipt = costReceipt(
      [
        call('reader', 1000, 200, 0.1),
        call('planner', 3000, 900, 0.2),
        call('reader', 500, 100, 0.2),
        call('judge', 800, 300, null),
      ],
      { inference_usd: 0.5, cloud_usd: 1.25, total_usd: 1.75 },
    )
    expect(receipt.rows).toEqual([
      { role: 'reader', calls: 2, tokensIn: 1500, tokensOut: 300, usd: 0.3, unpriced: 0 },
      { role: 'planner', calls: 1, tokensIn: 3000, tokensOut: 900, usd: 0.2, unpriced: 0 },
      { role: 'judge', calls: 1, tokensIn: 800, tokensOut: 300, usd: 0, unpriced: 1 },
    ])
    expect(receipt.tokensIn).toBe(5300)
    expect(receipt.tokensOut).toBe(1500)
    expect(receipt.inferenceUsd).toBe(0.5)
    expect(receipt.cloudUsd).toBe(1.25)
    expect(receipt.totalUsd).toBe(1.75)
    expect(receipt.unpricedCalls).toBe(1)
  })

  it('has no cloud spend before the first cost update', () => {
    const receipt = costReceipt([], null)
    expect(receipt).toMatchObject({ rows: [], inferenceUsd: 0, cloudUsd: 0, totalUsd: 0 })
  })
})
