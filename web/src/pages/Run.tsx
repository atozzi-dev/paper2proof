import { useEffect, useState } from 'react'
import { ApiError, api, isRunId } from '../api/client'
import { BundleDownload } from '../components/BundleDownload'
import { ClaimsTable } from '../components/ClaimsTable'
import { CostReceiptView } from '../components/CostReceiptView'
import { JobsList } from '../components/JobsList'
import { LogPanel } from '../components/LogPanel'
import { PlanCard } from '../components/PlanCard'
import { StatusTimeline } from '../components/StatusTimeline'
import { VerdictCard } from '../components/VerdictCard'
import { useRunStream } from '../run/useRunStream'
import { warningTitle } from '../run/verdictCopy'
import { href } from '../router'
import { TERMINAL_STATUSES, type RunView } from '../types/api'

export function RunPage({ id, replay }: { id: string; replay: boolean }) {
  if (!isRunId(id)) return <NotFound />
  return <RunPageInner key={`${id}:${replay}`} id={id} replay={replay} />
}

function NotFound() {
  return (
    <section>
      <h1>Run not found</h1>
      <p>
        Runs are private to the browser session that started them, unless they are in the{' '}
        <a href={href.gallery}>gallery</a>.
      </p>
    </section>
  )
}

function RunPageInner({ id, replay }: { id: string; replay: boolean }) {
  const [run, setRun] = useState<RunView | null>(null)
  const [loadError, setLoadError] = useState<ApiError | null>(null)
  const { state, mode } = useRunStream(id, replay, run !== null && TERMINAL_STATUSES.has(run.status))

  useEffect(() => {
    api.getRun(id).then(setRun, (err: unknown) =>
      setLoadError(err instanceof ApiError ? err : new ApiError(0, 'The run could not be loaded.')),
    )
  }, [id])

  // Refresh the summary (status, listed) once the stream reports the run is over, and the
  // verdict conditions as soon as a verdict is out (the bundle may still take minutes).
  const terminal = state.status !== null && TERMINAL_STATUSES.has(state.status)
  const verdictCount = Object.keys(state.verdicts).length
  useEffect(() => {
    if (terminal || verdictCount > 0) api.getRun(id).then(setRun, () => undefined)
  }, [id, terminal, verdictCount])

  if (loadError?.status === 404 || mode === 'not_found') return <NotFound />

  const status = state.status ?? run?.status ?? null
  const claims = state.claims.length ? state.claims : (run?.claims.map((c) => c.claim) ?? [])
  const tested = new Set(state.plan?.plan.claim_ids ?? run?.claims.filter((c) => c.selected).map((c) => c.claim.id))
  const verdicts = Object.keys(state.verdicts).length
    ? Object.entries(state.verdicts).map(([claimId, v]) => ({ claimId, ...v }))
    : (run?.verdicts.map((v) => ({ claimId: v.claim_id, verdict: v.verdict, explanation_md: '' })) ?? [])
  const claimById = new Map(claims.map((c) => [c.id, c]))
  // The tested claim in words, under the title: what this run is about.
  const headline = claims.find((c) => tested.has(c.id))?.text ?? claims[0]?.text ?? null

  return (
    <article className="run">
      <header className="run-header">
        <div className="run-header-main">
          <p className="muted small breadcrumb">
            <a href={href.gallery}>Gallery</a> / run <span className="mono">{id}</span>
            {run && !run.listed && ' · private to this browser session'}
            {run?.listed && ' · in the public gallery'}
          </p>
          <h1>
            arXiv{' '}
            {run ? (
              <a href={`https://arxiv.org/abs/${encodeURIComponent(run.arxiv_id)}`} rel="noreferrer noopener" target="_blank">
                {run.arxiv_id}
              </a>
            ) : (
              '…'
            )}
          </h1>
          {headline && <p className="run-subtitle">{headline}</p>}
        </div>
        {!replay && terminal && (
          <p className="run-actions">
            <a className="button ghost" href={href.run(id, true)}>
              ▶ Replay at 10×
            </a>
          </p>
        )}
        {replay && (
          <p className="banner">
            Replay of a recorded run at 10× speed. Nothing is executed. <a href={href.run(id)}>Show it without replay</a>
          </p>
        )}
        {loadError && <p role="alert" className="error">{loadError.message}</p>}
      </header>

      <StatusTimeline steps={state.steps} status={status} mode={mode} replay={replay} />

      {(state.warnings.length > 0 || state.errors.length > 0 || state.egressBlocked.length > 0) && (
        <section className="card notices-card" aria-labelledby="notices-h">
          <h2 id="notices-h">Notices</h2>
          <ul className="notices">
            {state.errors.map((e, i) => (
              <li key={`e${i}`} className="error">
                <strong>{e.code}</strong>: {e.message}
              </li>
            ))}
            {state.warnings.map((w, i) => (
              <li key={`w${i}`} className="warning">
                <strong>{warningTitle(w.code)}</strong>: {w.message}
              </li>
            ))}
            {state.egressBlocked.map((b, i) => (
              <li key={`b${i}`}>
                <strong>Egress blocked</strong>: {b.host} (from the {b.subnet} subnet)
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-labelledby="verdicts-h">
        <h2 id="verdicts-h" className="section-title">
          Verdicts
        </h2>
        {verdicts.length === 0 && <p className="muted">No verdict yet.</p>}
        <div className="verdicts">
          {verdicts.map((v) => {
            return (
              <VerdictCard
                key={v.claimId}
                claim={claimById.get(v.claimId) ?? null}
                claimId={v.claimId}
                verdict={v.verdict}
                explanationMd={v.explanation_md}
                plan={state.plan}
                warnings={state.warnings}
                jobs={state.jobs}
                conditions={run?.conditions ?? null}
              />
            )
          })}
        </div>
      </section>

      <div className="run-columns">
        <section className="card" aria-labelledby="cost-h">
          <h2 id="cost-h">Cost</h2>
          <p className="muted small">Every model call of this run, by role and model, on Nebius Token Factory.</p>
          <CostReceiptView calls={state.modelCalls} update={state.cost} />
        </section>

        <div className="run-side">
          {status === 'COMPLETED' && (
            <section className="card" aria-labelledby="bundle-h">
              <h2 id="bundle-h">Proof Bundle</h2>
              <p className="muted small">
                Everything needed to check and re-run this result: claims, plan, code, logs, measurements and the
                verdict configuration.
              </p>
              <BundleDownload runId={id} warnings={state.warnings} />
            </section>
          )}
          {state.plan && <PlanCard runId={id} plan={state.plan} approval={state.approval} readOnly={replay} />}
        </div>
      </div>

      <section className="card" aria-labelledby="claims-h">
        <h2 id="claims-h">Claims</h2>
        <ClaimsTable claims={claims} tested={tested} />
      </section>

      <section className="card" aria-labelledby="logs-h">
        <h2 id="logs-h">Logs</h2>
        <p className="muted small">Output of generated code, shown as plain text.</p>
        <div className="logs">
          <LogPanel title="Sandbox output" lines={state.sandboxLines} />
          <LogPanel title="Job logs" lines={state.jobLines} />
        </div>
      </section>

      <section className="card" aria-labelledby="jobs-h">
        <h2 id="jobs-h">Cloud jobs</h2>
        <JobsList jobs={state.jobs} />
      </section>
    </article>
  )
}
