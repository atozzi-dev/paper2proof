import { useEffect, useRef, useState } from 'react'
import { ApiError, api, isRunId } from '../api/client'
import { BundleDownload } from '../components/BundleDownload'
import { ClaimQuote } from '../components/ClaimQuote'
import { ClaimsTable } from '../components/ClaimsTable'
import { CostReceiptView } from '../components/CostReceiptView'
import { JobsList } from '../components/JobsList'
import { LogPanel } from '../components/LogPanel'
import { PlanCard } from '../components/PlanCard'
import { SetupTable } from '../components/SetupTable'
import { SpendCard } from '../components/SpendCard'
import { StatusTimeline } from '../components/StatusTimeline'
import { Tabs, type TabSpec } from '../components/Tabs'
import { VerdictCard } from '../components/VerdictCard'
import { costReceipt, formatUsd } from '../run/cost'
import { fmtDate, fmtDuration } from '../run/format'
import { CAUTION_ORDER, conditionChips, inShort } from '../run/summary'
import { useRunStream } from '../run/useRunStream'
import { warningTitle } from '../run/verdictCopy'
import { href } from '../router'
import { TERMINAL_STATUSES, type RunView } from '../types/api'

type AppendixTab = 'claims' | 'setup' | 'steps' | 'plan' | 'cost' | 'logs' | 'bundle'

const APPENDIX: readonly TabSpec<AppendixTab>[] = [
  { id: 'claims', label: 'A · Claims' },
  { id: 'setup', label: 'B · The paper’s setup' },
  { id: 'steps', label: 'C · Steps' },
  { id: 'plan', label: 'D · Plan' },
  { id: 'cost', label: 'E · Cost' },
  { id: 'logs', label: 'F · Logs' },
  { id: 'bundle', label: 'G · Proof Bundle' },
]

export function RunPage({ id, replay }: { id: string; replay: boolean }) {
  if (!isRunId(id)) return <NotFound />
  return <RunPageInner key={`${id}:${replay}`} id={id} replay={replay} />
}

function NotFound() {
  return (
    <section className="page-header-main">
      <h1 className="page-title">Run not found</h1>
      <p className="lead">
        Runs are private to the browser session that started them, unless they are in the{' '}
        <a href={href.gallery}>gallery</a>.
      </p>
    </section>
  )
}

function RunPageInner({ id, replay }: { id: string; replay: boolean }) {
  const [run, setRun] = useState<RunView | null>(null)
  const [loadError, setLoadError] = useState<ApiError | null>(null)
  const [tab, setTab] = useState<AppendixTab>('setup')
  const focusPart = useRef(false)
  const appendixRef = useRef<HTMLElement>(null)
  const { state, mode } = useRunStream(id, replay, run !== null && TERMINAL_STATUSES.has(run.status))

  useEffect(() => {
    api.getRun(id).then(loaded, (err: unknown) =>
      setLoadError(err instanceof ApiError ? err : new ApiError(0, 'The run could not be loaded.')),
    )
  }, [id])

  // A later load that succeeds clears an earlier failure.
  function loaded(view: RunView) {
    setRun(view)
    setLoadError(null)
  }

  // Refresh the summary (status, listed) once the stream reports the run is over, and the
  // verdict conditions as soon as a verdict is out (the bundle may still take minutes).
  const terminal = state.status !== null && TERMINAL_STATUSES.has(state.status)
  const verdictCount = Object.keys(state.verdicts).length
  useEffect(() => {
    if (terminal || verdictCount > 0) api.getRun(id).then(loaded, () => undefined)
  }, [id, terminal, verdictCount])

  // The header's Proof Bundle button moves focus to the part it opened, not only the view.
  useEffect(() => {
    if (!focusPart.current) return
    focusPart.current = false
    focusSelectedTab()
  }, [tab])

  if (loadError?.status === 404 || mode === 'not_found') return <NotFound />

  // A replay shows the run as the stream reaches it: nothing from its end shows early.
  const status = state.status ?? (replay ? null : (run?.status ?? null))
  const finished = status !== null && TERMINAL_STATUSES.has(status)
  const claims = state.claims.length ? state.claims : (run?.claims.map((c) => c.claim) ?? [])
  const tested = new Set(state.plan?.plan.claim_ids ?? run?.claims.filter((c) => c.selected).map((c) => c.claim.id))
  const verdicts = (
    Object.keys(state.verdicts).length
      ? Object.entries(state.verdicts).map(([claimId, v]) => ({ claimId, ...v }))
      : replay
        ? []
        : (run?.verdicts.map((v) => ({ claimId: v.claim_id, verdict: v.verdict, explanation_md: '' })) ?? [])
  ).sort((a, b) => CAUTION_ORDER.indexOf(a.verdict.kind) - CAUTION_ORDER.indexOf(b.verdict.kind))
  const claimById = new Map(claims.map((c) => [c.id, c]))
  // The tested claim in words, under the title: what this run is about.
  const headline = claims.find((c) => tested.has(c.id))?.text ?? claims[0]?.text ?? null
  const receipt = costReceipt(state.modelCalls, state.cost)
  const took = run?.finished_at ? Date.parse(run.finished_at) - Date.parse(run.created_at) : NaN
  const jobs = Object.keys(state.jobs).length
  const pendingPlan = state.plan !== null && state.approval?.pending === true
  const conditions = run?.conditions ?? null

  function focusSelectedTab() {
    appendixRef.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus()
  }

  function openPart(part: AppendixTab) {
    appendixRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
    if (part === tab) return focusSelectedTab()
    focusPart.current = true
    setTab(part)
  }

  return (
    <article className="run">
      <header className="run-header">
        <div className="run-header-main">
          <p className="kicker">
            {finished ? (
              <>
                <a href={href.gallery}>Gallery</a> · Report
              </>
            ) : (
              'Report in preparation'
            )}
            {run && ` · ${fmtDate(run.created_at)}`}
            {' · arXiv '}
            {run ? (
              <a href={`https://arxiv.org/abs/${encodeURIComponent(run.arxiv_id)}`} rel="noreferrer noopener" target="_blank">
                {run.arxiv_id}
              </a>
            ) : (
              '…'
            )}
            {run && !run.listed && ' · private to this browser session'}
          </p>
          <h1 className="run-title">arXiv {run?.arxiv_id ?? '…'}</h1>
          {headline && <p className="run-subtitle">{headline}</p>}
          <p className="run-meta">
            {Number.isFinite(took) && took >= 0 && `Re-run in ${fmtDuration(took)}${receipt.cloudReported ? ` for ${formatUsd(receipt.totalUsd)}` : ''} · `}
            run <span className="mono">{id}</span>
            {run?.listed && ' · in the public gallery'}
          </p>
        </div>
        <div className="run-actions">
          {!replay && finished && (
            <a className="button" href={href.run(id, true)}>
              ▶ Replay at 10×
            </a>
          )}
          {status === 'COMPLETED' && (
            <button type="button" className="dark" onClick={() => openPart('bundle')}>
              Proof Bundle
            </button>
          )}
        </div>
        {replay && (
          <p className="banner">
            Replay of a recorded run at 10× speed. Nothing is executed. <a href={href.run(id)}>Show it without replay</a>
          </p>
        )}
        {loadError && (
          <p role="alert" className="error">
            {loadError.message}
          </p>
        )}
      </header>

      {!finished && (
        <div className="progress-layout">
          <StatusTimeline steps={state.steps} status={status} mode={mode} replay={replay} />
          <aside className="progress-aside" aria-label="The run so far">
            <SpendCard calls={state.modelCalls} update={state.cost} jobs={jobs} estimateUsd={state.plan?.est_cost_usd ?? null} />
            {claims.filter((c) => tested.size === 0 || tested.has(c.id)).length > 0 && (
              <section className="under-test" aria-labelledby="under-test-h">
                <h2 id="under-test-h" className="kicker">
                  Claims under test
                </h2>
                {claims
                  .filter((c) => tested.size === 0 || tested.has(c.id))
                  .map((c) => (
                    <div key={c.id} className="under-test-claim">
                      <p className="muted small">
                        Claim {c.id} · {c.claim_type === 'ABSOLUTE' ? 'absolute' : 'relative'} · {c.metric}
                      </p>
                      <ClaimQuote claim={c} size="sm" />
                    </div>
                  ))}
              </section>
            )}
          </aside>
        </div>
      )}

      {(state.warnings.length > 0 || state.errors.length > 0 || state.egressBlocked.length > 0) && (
        <section className="notices" aria-labelledby="notices-h">
          <h2 id="notices-h" className="kicker">
            Notices
          </h2>
          <ul>
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

      {pendingPlan && state.plan && (
        <PlanCard runId={id} plan={state.plan} approval={state.approval} readOnly={replay} />
      )}

      {verdicts.length > 0 && (
        <section className="band in-short" aria-label="In short">
          <p className="kicker kicker-accent">In short</p>
          <p className="in-short-text">{inShort(verdicts.map((v) => v.verdict.kind))}</p>
          <ul className="chips" aria-label="Conditions of this run">
            {conditionChips(conditions, state.plan).map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </section>
      )}

      {verdicts.length > 0 ? (
        <section className="verdicts" aria-label="Verdicts">
          {verdicts.map((v) => (
            <VerdictCard
              key={v.claimId}
              claim={claimById.get(v.claimId) ?? null}
              claimId={v.claimId}
              verdict={v.verdict}
              explanationMd={v.explanation_md}
              plan={state.plan}
              warnings={state.warnings}
              jobs={state.jobs}
              conditions={conditions}
            />
          ))}
        </section>
      ) : (
        finished && <p className="muted">No verdict was reached: the notices and the steps in the appendix say where the run stopped.</p>
      )}

      <section ref={appendixRef} className="appendix" aria-labelledby="appendix-h">
        <div className="section-head">
          <h2 id="appendix-h">Appendix</h2>
          <p className="muted small">everything behind the verdict, one part at a time</p>
        </div>
        <Tabs label="Appendix parts" tabs={APPENDIX} selected={tab} onSelect={setTab}>
          {tab === 'claims' && <ClaimsTable claims={claims} tested={tested} />}
          {tab === 'setup' && (
            <div className="part">
              <p className="part-intro">
                The conditions the paper states, against the ones this run used. A number counts only when it is
                written in the code the run used; a choice is taken as the Coder declares it.
              </p>
              <SetupTable setup={conditions?.setup ?? []} />
              <p className="hand">one difference from the paper and the run drops to reduced scale: it can then confirm a gain, never accuse</p>
            </div>
          )}
          {tab === 'steps' && (
            <div className="part">
              {finished ? (
                <StatusTimeline steps={state.steps} status={status} mode={mode} replay={replay} level={3} />
              ) : (
                <p className="part-intro">The steps are listed at the top of the page while the run is going.</p>
              )}
              <h3>Cloud jobs</h3>
              <JobsList jobs={state.jobs} />
            </div>
          )}
          {tab === 'plan' &&
            (pendingPlan ? (
              // one decision, in one place: the pending plan stays above, outside the tabs
              <p className="part-intro">The plan is shown above, waiting for approval.</p>
            ) : state.plan ? (
              <PlanCard runId={id} plan={state.plan} approval={state.approval} readOnly={replay} level={3} />
            ) : (
              <p className="muted">No plan yet.</p>
            ))}
          {tab === 'cost' && (
            <div className="part">
              <p className="part-intro">Every model call of this run, by role and model, on Nebius Token Factory, and the cloud jobs.</p>
              <CostReceiptView calls={state.modelCalls} update={state.cost} />
            </div>
          )}
          {tab === 'logs' && (
            <div className="part">
              <p className="part-intro">Output of generated code, shown as plain text.</p>
              <div className="logs">
                <LogPanel title="Sandbox output" lines={state.sandboxLines} />
                <LogPanel title="Job logs" lines={state.jobLines} />
              </div>
            </div>
          )}
          {tab === 'bundle' &&
            (status === 'COMPLETED' ? (
              <div className="part bundle-part">
                <div className="part-main">
                  <p>
                    Everything needed to check and re-run this result: claims, plan, code, logs, measurements and the
                    verdict configuration. Every file is hashed into a signed manifest; the public key is in the
                    repository.
                  </p>
                  <pre>uv run python -m app.bundle.verify proof-bundle-{id}.tar.gz</pre>
                </div>
                <BundleDownload runId={id} warnings={state.warnings} />
              </div>
            ) : (
              <p className="muted">The Proof Bundle is built when the run completes.</p>
            ))}
        </Tabs>
      </section>
    </article>
  )
}
