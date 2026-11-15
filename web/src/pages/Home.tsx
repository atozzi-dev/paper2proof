import { useEffect, useId, useState, type FormEvent } from 'react'
import { ARXIV_ID, ApiError, api } from '../api/client'
import { CiBandChart } from '../components/CiBandChart'
import { ClaimQuote } from '../components/ClaimQuote'
import { GalleryList } from '../components/GalleryList'
import { VerdictFigures } from '../components/VerdictFigures'
import { VerdictPill } from '../components/VerdictPill'
import { fmtDate } from '../run/format'
import { CAUTION_ORDER } from '../run/summary'
import { verdictConditions } from '../run/verdictCopy'
import { href, navigate } from '../router'
import type { RunCard, RunView } from '../types/api'

const STEPS = [
  {
    title: 'Read',
    body: 'Nemotron models read the paper and pick the claims a run can test, each with the place in the paper it comes from.',
    chip: 'Reader · Nemotron 3.5 Super VL',
    figure: <ReadFigure />,
  },
  {
    title: 'Re-run',
    body: 'The experiment is planned, written from the paper, smoke-tested and run over several seeds in an isolated Nebius Token Factory sandbox, without internet.',
    chip: 'Planner · Nemotron Ultra · Coder · Nemotron 3.5 Super VL',
    figure: <RerunFigure />,
  },
  {
    title: 'Decide',
    body: "A fixed statistical rule, not a model, compares our 95% interval with the paper's value. The Judge model only explains the verdict.",
    chip: 'A rule, not a model',
    figure: <DecideFigure />,
  },
]

const ARROW = (
  <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden="true">
    <path d="M3 8 H12 M8.5 4 L12.5 8 L8.5 12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
  </svg>
)

const TICK = (
  <svg width="18" height="18" viewBox="0 0 30 30" aria-hidden="true">
    <path d="M10 15.5l3.5 3.5L20.5 11" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

export function HomePage() {
  const ids = { arxiv: useId(), invite: useId(), hint: useId(), help: useId(), error: useId() }
  const [arxivId, setArxivId] = useState('')
  const [invite, setInvite] = useState('')
  const [hint, setHint] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [examples, setExamples] = useState<RunCard[]>([])
  const [latest, setLatest] = useState<RunView | null>(null)

  useEffect(() => {
    api.gallery().then((cards) => setExamples(cards.slice(0, 4)), () => setExamples([]))
  }, [])

  // The newest gallery run, in full: its report is the page's example of what a run returns.
  const latestId = examples[0]?.id
  useEffect(() => {
    if (latestId) api.getRun(latestId).then(setLatest, () => setLatest(null))
  }, [latestId])

  async function submit(e: FormEvent) {
    e.preventDefault()
    const id = arxivId.trim()
    if (!ARXIV_ID.test(id)) {
      setError('Enter an arXiv ID such as 2106.09685 or 2106.09685v2. URLs and PDFs are not accepted.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (invite.trim()) await api.createSession(invite.trim())
      const created = await api.createRun(id, hint.trim() || null)
      navigate(href.run(created.run_id))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The run could not be started. Try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <section className="hero" aria-labelledby="hero-h">
        <div className="hero-main">
          <h1 id="hero-h" className="hero-title">
            Do the numbers in this paper{' '}
            <span className="hero-accent">
              hold up?
              <svg viewBox="0 0 300 20" preserveAspectRatio="none" aria-hidden="true">
                <path d="M4 13 C 70 3, 170 3, 296 11" fill="none" stroke="currentColor" strokeWidth="5" strokeLinecap="round" />
              </svg>
            </span>
          </h1>
          <p className="lead hero-lead">
            Give it an arXiv ID. We re-run the experiment behind each claim, over several seeds, in a sandbox with no
            network. A fixed statistical rule decides; a model only explains.
          </p>
          <ul className="hero-ticks" aria-label="What you get">
            <li>{TICK}a verdict per claim</li>
            <li>{TICK}the conditions it was reached under</li>
            <li>{TICK}a signed Proof Bundle anyone can verify</li>
          </ul>
        </div>

        <div className="hero-side">
          <form className="start-form" onSubmit={submit} noValidate aria-labelledby="start-h" aria-describedby={error ? ids.error : undefined}>
            <h2 id="start-h">Start a run</h2>
            <label htmlFor={ids.arxiv}>arXiv ID</label>
            <input
              id={ids.arxiv}
              className="arxiv-input"
              name="arxiv_id"
              inputMode="decimal"
              autoComplete="off"
              spellCheck={false}
              placeholder="2106.09685"
              maxLength={32}
              value={arxivId}
              onChange={(e) => setArxivId(e.target.value)}
              aria-describedby={ids.help}
              aria-invalid={error !== null && !ARXIV_ID.test(arxivId.trim())}
              required
            />
            <label htmlFor={ids.invite}>Invite code</label>
            <input
              id={ids.invite}
              name="invite_code"
              type="password"
              autoComplete="off"
              value={invite}
              onChange={(e) => setInvite(e.target.value)}
            />
            <details className="hint-disclosure">
              <summary>Focus on one claim</summary>
              <label htmlFor={ids.hint}>Claim to focus on (optional)</label>
              <textarea
                id={ids.hint}
                name="claim_hint"
                rows={2}
                maxLength={500}
                value={hint}
                onChange={(e) => setHint(e.target.value)}
              />
            </details>
            <button type="submit" className="primary start-button" disabled={busy}>
              {busy ? 'Starting…' : 'Start the run'}
              {!busy && ARROW}
            </button>
            {error && (
              <p id={ids.error} role="alert" className="error">
                {error}
              </p>
            )}
            <p id={ids.help} className="form-note">
              arXiv IDs only, such as 2106.09685 or 2106.09685v2: no URLs, no PDF uploads. Live runs use real compute,
              so they need an invite code, once per browser session. Replays in the gallery are free.
            </p>
            <p className="form-note">Runs are private to your browser session. The public gallery is curated by hand.</p>
          </form>
          <p className="hand hero-note">
            <svg width="34" height="26" viewBox="0 0 34 26" aria-hidden="true">
              <path d="M30 22 C 20 22, 10 16, 6 5 M2 10 L6 4 L11 9" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            in our test set: under 2 minutes, about 2¢ a claim
          </p>
        </div>
      </section>

      {latest && <LatestReport run={latest} />}

      <section className="how-band" aria-labelledby="how-h">
        <div className="section-row">
          <h2 id="how-h">From paper to proof, in three figures</h2>
          <a href={href.about}>The whole method →</a>
        </div>
        <ol className="how-figures" aria-label="How it works">
          {STEPS.map((s, i) => (
            <li key={s.title}>
              {s.figure}
              <h3>
                <span className="fig-label">Fig. {i + 1}</span> {s.title}
              </h3>
              <p className="muted-2">{s.body}</p>
              <p className="how-chip">{s.chip}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="recent" aria-labelledby="examples-h">
        <div className="section-head">
          <h2 id="examples-h">Recent reports</h2>
          <a href={href.gallery}>Open the gallery →</a>
        </div>
        {examples.length === 0 ? (
          <p className="muted">
            No reports yet. See the <a href={href.gallery}>gallery</a>.
          </p>
        ) : (
          <GalleryList cards={examples} />
        )}
      </section>
    </>
  )
}

/** The newest gallery report, verdict first: what a run returns, before starting one. */
function LatestReport({ run }: { run: RunView }) {
  const claimOf = (id: string) => run.claims.find((c) => c.claim.id === id)?.claim ?? null
  // The most cautionary verdict leads: a "not reproduced" is never hidden behind a success.
  const ordered = [...run.verdicts].sort(
    (a, b) => CAUTION_ORDER.indexOf(a.verdict.kind) - CAUTION_ORDER.indexOf(b.verdict.kind),
  )
  const first = ordered[0]
  if (!first) return null
  const claim = claimOf(first.claim_id)
  const conditions = verdictConditions(first.verdict, {
    plan: null,
    warnings: [],
    jobs: {},
    conditions: run.conditions,
    claimId: first.claim_id,
  })
  return (
    <section className="latest" aria-label="Latest report">
      <div className="section-head">
        <p className="kicker">
          From the gallery · report of {fmtDate(run.created_at)} · arXiv <span className="mono">{run.arxiv_id}</span>
        </p>
        <a href={href.gallery}>All reports →</a>
      </div>
      <h2 className="latest-title">arXiv {run.arxiv_id}</h2>
      <div className="report-columns">
        <article className="report-main" aria-labelledby="latest-verdict">
          <div className="verdict-line">
            <VerdictPill id="latest-verdict" kind={first.verdict.kind} as="h3" size="lg" />
            {claim && (
              <span className="muted small">
                Claim {claim.id} · {claim.source_ref}
              </span>
            )}
          </div>
          {claim && <ClaimQuote claim={claim} />}
          <VerdictFigures claim={claim} verdict={first.verdict} size="md" />
          <CiBandChart claim={claim} verdict={first.verdict} />
          {ordered.slice(1).map((v) => {
            const other = claimOf(v.claim_id)
            return (
              <div key={v.claim_id} className="other-verdict">
                <VerdictPill kind={v.verdict.kind} />
                {other && <ClaimQuote claim={other} size="sm" />}
              </div>
            )
          })}
        </article>
        <aside className="report-aside" aria-label="Conditions of this report">
          <p className="kicker">Under these conditions</p>
          <ul className="conditions" aria-label="Conditions">
            {conditions.map((c) => (
              <li key={c.label}>
                <strong>{c.label}:</strong> {c.value}
              </li>
            ))}
          </ul>
          <a className="button outline" href={href.run(run.id)}>
            Read the full report {ARROW}
          </a>
        </aside>
      </div>
    </section>
  )
}

function ReadFigure() {
  return (
    <svg className="how-figure" viewBox="0 0 200 110" aria-hidden="true">
      <path d="M62 8 H124 L142 26 V102 H62 Z M124 8 V26 H142" fill="none" stroke="var(--accent)" strokeWidth="2.5" strokeLinejoin="round" />
      <rect x="72" y="52" width="58" height="11" rx="2" fill="var(--mark)" />
      <g stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round">
        <line x1="74" y1="38" x2="118" y2="38" />
        <line x1="74" y1="57" x2="128" y2="57" />
        <line x1="74" y1="76" x2="126" y2="76" />
        <line x1="74" y1="89" x2="108" y2="89" />
      </g>
      <path d="M150 50 L160 60 L178 38" fill="none" stroke="var(--ink)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function RerunFigure() {
  return (
    <svg className="how-figure" viewBox="0 0 200 110" aria-hidden="true">
      <rect x="46" y="18" width="108" height="80" rx="14" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
      <circle cx="76" cy="66" r="9" fill="var(--accent)" />
      <circle cx="100" cy="66" r="9" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
      <circle cx="124" cy="66" r="9" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
      <g fill="none" stroke="var(--ink)" strokeWidth="2.5" strokeLinecap="round">
        <path d="M162 24 a14 14 0 0 1 22 0" />
        <path d="M167 30 a7 7 0 0 1 12 0" />
        <line x1="160" y1="40" x2="186" y2="14" />
      </g>
      <text x="100" y="40" textAnchor="middle" className="how-figure-hand">
        seeds 0 · 1 · 2
      </text>
    </svg>
  )
}

function DecideFigure() {
  return (
    <svg className="how-figure" viewBox="0 0 200 110" aria-hidden="true">
      <rect x="92" y="34" width="96" height="40" rx="6" fill="var(--ok-bg)" />
      <line x1="20" y1="74" x2="188" y2="74" stroke="var(--ink)" strokeWidth="2" />
      <line x1="111" y1="22" x2="111" y2="74" stroke="var(--ink)" strokeWidth="2" />
      <rect x="92" y="10" width="38" height="13" rx="3" fill="var(--mark)" />
      <line x1="98" y1="54" x2="144" y2="54" stroke="var(--accent)" strokeWidth="4" strokeLinecap="round" />
      <circle cx="121" cy="54" r="6" fill="var(--accent)" />
      <path d="M40 92 L48 100 L62 86" fill="none" stroke="var(--ok)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
