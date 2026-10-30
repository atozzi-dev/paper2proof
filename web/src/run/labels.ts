import type { JobKind, RunStatus, StepName } from '../types/api'

export const PIPELINE: { step: StepName; label: string }[] = [
  { step: 'INGESTING', label: 'Ingesting the paper' },
  { step: 'EXTRACTING_CLAIMS', label: 'Extracting claims' },
  { step: 'DISCOVERING', label: 'Discovering code and data' },
  { step: 'PLANNING', label: 'Planning the experiment' },
  { step: 'AWAITING_APPROVAL', label: 'Awaiting approval' },
  { step: 'PROVISIONING', label: 'Provisioning packages and data' },
  { step: 'SMOKE_TESTING', label: 'Smoke testing in the sandbox' },
  { step: 'EXECUTING', label: 'Running the experiment' },
  { step: 'ANALYZING', label: 'Analyzing results' },
  { step: 'VERDICT', label: 'Verdict' },
]

export const STATUS_TEXT: Record<RunStatus, string> = {
  QUEUED: 'Queued',
  RUNNING: 'Running',
  COMPLETED: 'Completed',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
  BUDGET_EXCEEDED: 'Stopped: budget reached',
}

/** What each cloud job does (the job kinds of app.execution.backend.JobKind). */
export const JOB_KIND_TEXT: Record<JobKind, string> = {
  prov: 'Packages (provision job)',
  fetch: 'Repository and datasets (fetch job)',
  smoke: 'Smoke test (smoke job)',
  exec: 'Experiment (exec job)',
}
