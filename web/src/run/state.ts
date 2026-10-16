// Run page state, rebuilt from the event stream (pure reducer: replay-safe and testable).
import type {
  Claim,
  CostUpdate,
  ErrorCode,
  EventPayloads,
  ModelCall,
  PlanReady,
  RunEvent,
  RunStatus,
  StepName,
  Verdict,
  Warning,
} from '../types/api'

/** Untrusted output lines kept per log; older ones are dropped. */
export const MAX_LOG_LINES = 5000

export interface StepEntry {
  step: StepName
  attempt: number
  state: 'running' | 'ok' | 'failed'
  error_code?: ErrorCode | null
  duration_ms?: number
}

export interface LogLine {
  key: number
  /** Short origin label, e.g. "iter 2 · stderr" or "exec". */
  source: string
  stderr: boolean
  text: string
}

export interface VerdictEntry {
  verdict: Verdict
  explanation_md: string
}

export interface RunState {
  lastEventId: number
  status: RunStatus | null
  errorCode: ErrorCode | null
  steps: StepEntry[]
  claims: Claim[]
  plan: PlanReady | null
  approval: { expires_at: string; pending: boolean } | null
  modelCalls: ModelCall[]
  cost: CostUpdate | null
  sandboxLines: LogLine[]
  jobLines: LogLine[]
  jobs: Record<string, EventPayloads['job.status']>
  egressBlocked: EventPayloads['egress.blocked'][]
  verdicts: Record<string, VerdictEntry>
  warnings: Warning[]
  errors: EventPayloads['error'][]
}

export function initialRunState(): RunState {
  return {
    lastEventId: 0,
    status: null,
    errorCode: null,
    steps: [],
    claims: [],
    plan: null,
    approval: null,
    modelCalls: [],
    cost: null,
    sandboxLines: [],
    jobLines: [],
    jobs: {},
    egressBlocked: [],
    verdicts: {},
    warnings: [],
    errors: [],
  }
}

function appendLines(log: LogLine[], firstKey: number, source: string, stderr: boolean, lines: string[]) {
  const added = lines.map((text, i) => ({ key: firstKey + i, source, stderr, text }))
  const next = log.concat(added)
  return next.length > MAX_LOG_LINES ? next.slice(next.length - MAX_LOG_LINES) : next
}

function upsertStep(steps: StepEntry[], entry: StepEntry): StepEntry[] {
  const i = steps.findIndex((s) => s.step === entry.step && s.attempt === entry.attempt)
  if (i === -1) return [...steps, entry]
  const next = steps.slice()
  next[i] = { ...steps[i], ...entry }
  return next
}

export function reduceRun(state: RunState, event: RunEvent): RunState {
  if (event.id <= state.lastEventId) return state
  const s: RunState = { ...state, lastEventId: event.id }
  // Log line keys: event id * 100 + index stays unique (≤ 50 lines per event).
  const lineKey = event.id * 100
  switch (event.type) {
    case 'run.status':
      return { ...s, status: event.payload.status, errorCode: event.payload.error_code ?? s.errorCode }
    case 'step.started':
      return { ...s, steps: upsertStep(s.steps, { step: event.payload.step, attempt: event.payload.attempt, state: 'running' }) }
    case 'step.finished': {
      const p = event.payload
      const steps = upsertStep(s.steps, {
        step: p.step,
        attempt: p.attempt,
        state: p.ok ? 'ok' : 'failed',
        error_code: p.error_code ?? null,
        duration_ms: p.duration_ms,
      })
      const approval = p.step === 'AWAITING_APPROVAL' && s.approval ? { ...s.approval, pending: false } : s.approval
      return { ...s, steps, approval }
    }
    case 'claim.extracted': {
      const claims = s.claims.filter((c) => c.id !== event.payload.claim.id)
      return { ...s, claims: [...claims, event.payload.claim] }
    }
    case 'plan.ready':
      return { ...s, plan: event.payload }
    case 'approval.required':
      return { ...s, approval: { expires_at: event.payload.expires_at, pending: true } }
    case 'model.call':
      return { ...s, modelCalls: [...s.modelCalls, event.payload] }
    case 'sandbox.output': {
      const p = event.payload
      const branch = p.branch === null || p.branch === undefined ? '' : ` · branch ${p.branch}`
      const source = `iter ${p.iteration}${branch} · ${p.stream}`
      return { ...s, sandboxLines: appendLines(s.sandboxLines, lineKey, source, p.stream === 'stderr', p.lines) }
    }
    case 'job.status':
      return { ...s, jobs: { ...s.jobs, [event.payload.name]: event.payload } }
    case 'job.log':
      return { ...s, jobLines: appendLines(s.jobLines, lineKey, event.payload.kind, false, event.payload.lines) }
    case 'egress.blocked':
      return { ...s, egressBlocked: [...s.egressBlocked, event.payload] }
    case 'verdict.ready':
      return {
        ...s,
        verdicts: {
          ...s.verdicts,
          [event.payload.claim_id]: { verdict: event.payload.verdict, explanation_md: event.payload.explanation_md },
        },
      }
    case 'cost.update':
      return { ...s, cost: event.payload }
    case 'warning':
      return { ...s, warnings: [...s.warnings, event.payload] }
    case 'error':
      return { ...s, errors: [...s.errors, event.payload] }
  }
}
