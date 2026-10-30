import type { Claim, Plan, RunConditions, RunEvent, Verdict } from '../src/types/api'

export const claimAbs: Claim = {
  id: 'c1',
  text: 'Our method reaches 94.1% top-1 accuracy on CIFAR-10.',
  claim_type: 'ABSOLUTE',
  metric: 'accuracy',
  higher_is_better: true,
  dataset: 'CIFAR-10',
  setting: 'ResNet-18, 200 epochs',
  reported_value: 0.941,
  reported_std: null,
  baseline_name: null,
  claimed_delta: null,
  source_ref: 'Table 2',
}

export const claimRel: Claim = {
  ...claimAbs,
  id: 'c2',
  text: 'Our method beats ResNet-18 by 1.7 points.',
  claim_type: 'RELATIVE',
  reported_value: null,
  baseline_name: 'ResNet-18',
  claimed_delta: 0.017,
  source_ref: 'Sec 4.1',
}

export const plan: Plan = {
  mode: 'REPO_FIRST',
  claim_ids: ['c1', 'c2'],
  claim_types: { c1: 'ABSOLUTE', c2: 'RELATIVE' },
  scale: 'FULL',
  seeds: [0, 1, 2],
  packages: [{ name: 'timm', specifier: '==1.0.9' }],
  datasets: [{ kind: 'hf', id: 'uoft-cs/cifar10', split: 'test', max_bytes: 2_000_000_000 }],
  entrypoint_hint: 'train.py',
  gpu_minutes_est: 12,
  notes: ['Learning-rate schedule not specified in the paper: cosine used.'],
}

/** What GET /api/runs/{id} reports once the results are analyzed (app/api/conditions.py). */
export const runConditions: RunConditions = {
  attempt: 1,
  scale: { scale: 'REDUCED', fraction_of_data: 0.1, epochs: 5, paper_epochs: 200 },
  seeds_planned: [0, 1, 2],
  seeds_used: [0, 1],
  seeds_used_by_claim: { c1: [0, 1], c2: [0] },
  hardware: { platform: 'gpu-l40s-a', preset: '1gpu-8vcpu-32gb', preemptible: true, gpu: 'NVIDIA L40S' },
  code: 'FROM_SCRATCH',
  deviations: ['Smaller model than the paper.'],
  exit_reason: 'completed',
}

export const notReproduced: Verdict = {
  kind: 'NOT_REPRODUCED',
  reason: null,
  n_seeds: 3,
  mean: 0.912,
  std: 0.002,
  ci_low: 0.907,
  ci_high: 0.917,
  tolerance: { w: 0.01882, v: 0.941 },
}

export const reproduced: Verdict = {
  kind: 'REPRODUCED',
  reason: null,
  n_seeds: 3,
  mean: 0.02,
  std: 0.001,
  ci_low: 0.018,
  ci_high: 0.022,
  tolerance: { rho: 0.5, delta: 0.017 },
}

let nextId = 1
export function ev<T extends RunEvent['type']>(
  type: T,
  payload: Extract<RunEvent, { type: T }>['payload'],
): RunEvent {
  return { id: nextId++, type, payload } as RunEvent
}

/** A complete happy-path run, in server order. */
export function fullRun(): RunEvent[] {
  nextId = 1
  return [
    ev('run.status', { status: 'RUNNING' }),
    ev('step.started', { step: 'INGESTING', attempt: 1 }),
    ev('step.finished', { step: 'INGESTING', attempt: 1, ok: true, duration_ms: 1200 }),
    ev('step.started', { step: 'EXTRACTING_CLAIMS', attempt: 1 }),
    ev('model.call', {
      role: 'reader',
      model_id: 'reader-model',
      tokens_in: 1000,
      tokens_out: 200,
      latency_ms: 900,
      cost_usd: 0.01,
    }),
    ev('claim.extracted', { claim: claimAbs }),
    ev('claim.extracted', { claim: claimRel }),
    ev('step.finished', { step: 'EXTRACTING_CLAIMS', attempt: 1, ok: true, duration_ms: 3000 }),
    ev('plan.ready', { plan, est_cost_usd: 1.25, needs_approval: true }),
    ev('approval.required', { expires_at: '2026-10-16T13:00:00Z' }),
    ev('step.started', { step: 'AWAITING_APPROVAL', attempt: 1 }),
    ev('step.finished', { step: 'AWAITING_APPROVAL', attempt: 1, ok: true, duration_ms: 4000 }),
    ev('sandbox.output', { iteration: 0, stream: 'stdout', lines: ['<b>epoch 1</b>', 'ok'] }),
    ev('job.status', { kind: 'exec', name: 'p2p-exec-abc-1', state: 'RUNNING' }),
    ev('job.log', { kind: 'exec', lines: ['seed 0: 0.912'] }),
    ev('warning', { code: 'JOB_PREEMPTED', message: 'The GPU job was preempted: retried once on demand.' }),
    ev('verdict.ready', { claim_id: 'c1', verdict: notReproduced, explanation_md: 'The **mean** is below the band.' }),
    ev('verdict.ready', { claim_id: 'c2', verdict: reproduced, explanation_md: 'The gain holds.' }),
    ev('cost.update', { inference_usd: 0.01, cloud_usd: 0.4, total_usd: 0.41 }),
    ev('run.status', { status: 'COMPLETED' }),
  ]
}
