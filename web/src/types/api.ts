// Friendly names for the generated OpenAPI types (src/types/openapi.ts, `npm run gen:types`).
import type { components } from './openapi'

type S = components['schemas']

export type Claim = S['Claim']
export type ClaimType = S['ClaimType']
export type ErrorCode = S['ErrorCode']
export type Plan = S['Plan']
export type ReasonCode = S['ReasonCode']
export type RunCard = S['RunCard']
export type RunConditions = S['RunConditions']
export type RunStatus = S['RunStatus']
export type Scale = S['Scale']
export type RunView = S['RunView']
export type Stats = S['Stats']
export type StepName = S['StepName']
export type Verdict = S['Verdict']
export type VerdictKind = S['VerdictKind']
export type EventEnvelope = S['EventEnvelope']
export type JobKind = S['JobStatusPayload']['kind']

/** One payload type per SSE event type (docs/specs/api-and-events.md). */
export interface EventPayloads {
  'run.status': S['RunStatusPayload']
  'step.started': S['StepStartedPayload']
  'step.finished': S['StepFinishedPayload']
  'claim.extracted': S['ClaimExtractedPayload']
  'plan.ready': S['PlanReadyPayload']
  'approval.required': S['ApprovalRequiredPayload']
  'model.call': S['ModelCallPayload']
  'sandbox.output': S['SandboxOutputPayload']
  'job.status': S['JobStatusPayload']
  'job.log': S['JobLogPayload']
  'egress.blocked': S['EgressBlockedPayload']
  'verdict.ready': S['VerdictReadyPayload']
  'cost.update': S['CostUpdatePayload']
  warning: S['WarningPayload']
  error: S['ErrorPayload']
}

export type EventType = keyof EventPayloads

/** A run event narrowed by its type, so `event.payload` is typed after a `switch (event.type)`. */
export type RunEvent = {
  [K in EventType]: { id: number; type: K; payload: EventPayloads[K] }
}[EventType]

export type ModelCall = EventPayloads['model.call']
export type CostUpdate = EventPayloads['cost.update']
export type PlanReady = EventPayloads['plan.ready']
export type Warning = EventPayloads['warning']

const EVENT_TYPES: ReadonlySet<string> = new Set<EventType>([
  'run.status',
  'step.started',
  'step.finished',
  'claim.extracted',
  'plan.ready',
  'approval.required',
  'model.call',
  'sandbox.output',
  'job.status',
  'job.log',
  'egress.blocked',
  'verdict.ready',
  'cost.update',
  'warning',
  'error',
])

export function isEventType(value: string): value is EventType {
  return EVENT_TYPES.has(value)
}

/** Build a typed event; unknown types (a newer server) are dropped, never guessed. */
export function toRunEvent(id: number, type: string, payload: unknown): RunEvent | null {
  if (!Number.isInteger(id) || id < 1 || !isEventType(type) || typeof payload !== 'object' || payload === null) {
    return null
  }
  return { id, type, payload } as RunEvent
}

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'BUDGET_EXCEEDED',
])
