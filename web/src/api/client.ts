// Typed calls to the HTTP API (docs/specs/api-and-events.md). Same origin, cookie session.
import type { RunCard, RunView, Stats } from '../types/api'

export const ARXIV_ID = /^\d{4}\.\d{4,5}(v\d+)?$/ // invariant 11: arXiv IDs only
const RUN_ID = /^r_[0-9a-z]{26}$/

export function isRunId(value: string): boolean {
  return RUN_ID.test(value)
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

const FALLBACK: Record<number, string> = {
  400: 'The request was not valid.',
  401: 'An invite code is needed to start a run.',
  402: "Today's budget is used up: new runs are paused until tomorrow.",
  404: 'Not found. Runs are private to the browser session that started them.',
  409: 'The run is no longer waiting for a decision.',
  429: 'Too many requests or the daily quota of this invite is used up. Try again later.',
  503: 'The service is being updated. Try again in a couple of minutes.',
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(init.body ? { 'Content-Type': 'application/json' } : {}) },
  })
  if (!res.ok) {
    let message = FALLBACK[res.status] ?? `Request failed (${res.status}).`
    try {
      const body = (await res.json()) as { error?: unknown }
      if (typeof body.error === 'string' && res.status !== 404) message = body.error
    } catch {
      // keep the fallback message
    }
    throw new ApiError(res.status, message)
  }
  const text = await res.text() // 204, and 202 from cancel, have no body
  return (text ? JSON.parse(text) : undefined) as T
}

const runPath = (id: string) => `/api/runs/${encodeURIComponent(id)}`

export const api = {
  createSession: (inviteCode: string) =>
    request<void>('/api/session', { method: 'POST', body: JSON.stringify({ invite_code: inviteCode }) }),
  createRun: (arxivId: string, claimHint: string | null) =>
    request<{ run_id: string; events_url: string }>('/api/runs', {
      method: 'POST',
      body: JSON.stringify({ arxiv_id: arxivId, claim_hint: claimHint }),
    }),
  getRun: (id: string) => request<RunView>(runPath(id)),
  approve: (id: string) => request<void>(`${runPath(id)}/approve`, { method: 'POST' }),
  reject: (id: string) => request<void>(`${runPath(id)}/reject`, { method: 'POST' }),
  gallery: () => request<RunCard[]>('/api/gallery'),
  stats: () => request<Stats>('/api/stats'),
}

export const bundleUrl = (id: string) => `${runPath(id)}/bundle`
