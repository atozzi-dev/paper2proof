import type { Claim } from '../types/api'

export interface Segment {
  text: string
  mark: boolean
}

// A number, then an optional unit that scales it by 100 (94.1% for 0.941, 1.7 points for 0.017).
const NUMBER = /(\d+(?:\.\d+)?)(%|\s?(?:percentage points|points|pp)\b)?/g
// A spread written after the value: "0.966 ± 0.005", "94.1 ± 0.3%".
const SPREAD = /^\s*±\s*\d+(?:\.\d+)?%?/

function same(a: number, b: number): boolean {
  return Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(b))
}

/** The claim's text split around the paper's own value (the reported value, or the claimed
 * gain), so it can be shown highlighted. Pure text in, pure text out: no match, no mark. */
export function splitHighlight(text: string, claim: Claim): Segment[] {
  const raw = claim.claim_type === 'ABSOLUTE' ? claim.reported_value : claim.claimed_delta
  if (raw === null || raw === undefined || !Number.isFinite(raw)) return [{ text, mark: false }]
  const target = Math.abs(raw)
  for (const m of text.matchAll(NUMBER)) {
    const value = Number(m[1])
    const hit = m[2] ? same(value / 100, target) || same(value, target) : same(value, target)
    if (!hit) continue
    const start = m.index
    let end = start + m[0].length
    end += SPREAD.exec(text.slice(end))?.[0].length ?? 0
    return [
      { text: text.slice(0, start), mark: false },
      { text: text.slice(start, end), mark: true },
      { text: text.slice(end), mark: false },
    ].filter((s) => s.text !== '')
  }
  return [{ text, mark: false }]
}
