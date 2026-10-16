import type { CostUpdate, ModelCall } from '../types/api'

export interface RoleCost {
  role: string
  calls: number
  tokensIn: number
  tokensOut: number
  usd: number
  /** Calls whose model has no price in config/pricing.yaml (cost_usd = null). */
  unpriced: number
}

export interface CostReceipt {
  rows: RoleCost[]
  tokensIn: number
  tokensOut: number
  inferenceUsd: number
  cloudUsd: number
  totalUsd: number
  unpricedCalls: number
}

// Sum in micro-dollars so the rows add up exactly to the totals shown.
const MICRO = 1_000_000
const toMicro = (usd: number) => Math.round(usd * MICRO)
const fromMicro = (micro: number) => micro / MICRO

/** Tokens and dollars per role (first-seen order) from model.call events, plus cloud spend. */
export function costReceipt(calls: readonly ModelCall[], update: CostUpdate | null): CostReceipt {
  const byRole = new Map<string, { row: RoleCost; micro: number }>()
  for (const call of calls) {
    let entry = byRole.get(call.role)
    if (!entry) {
      entry = { row: { role: call.role, calls: 0, tokensIn: 0, tokensOut: 0, usd: 0, unpriced: 0 }, micro: 0 }
      byRole.set(call.role, entry)
    }
    entry.row.calls += 1
    entry.row.tokensIn += call.tokens_in
    entry.row.tokensOut += call.tokens_out
    if (call.cost_usd === null || call.cost_usd === undefined) entry.row.unpriced += 1
    else entry.micro += toMicro(call.cost_usd)
  }
  const rows = [...byRole.values()].map(({ row, micro }) => ({ ...row, usd: fromMicro(micro) }))
  const inferenceMicro = [...byRole.values()].reduce((sum, e) => sum + e.micro, 0)
  const cloudMicro = update ? toMicro(update.cloud_usd) : 0
  return {
    rows,
    tokensIn: rows.reduce((sum, r) => sum + r.tokensIn, 0),
    tokensOut: rows.reduce((sum, r) => sum + r.tokensOut, 0),
    inferenceUsd: fromMicro(inferenceMicro),
    cloudUsd: fromMicro(cloudMicro),
    totalUsd: fromMicro(inferenceMicro + cloudMicro),
    unpricedCalls: rows.reduce((sum, r) => sum + r.unpriced, 0),
  }
}

export function formatUsd(usd: number): string {
  if (usd !== 0 && Math.abs(usd) < 0.01) return `$${usd.toFixed(4)}`
  return `$${usd.toFixed(2)}`
}
