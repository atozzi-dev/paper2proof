import { useEffect, useId, useState, type FormEvent } from 'react'
import { ARXIV_ID, ApiError, api } from '../api/client'
import { CiBandChart } from '../components/CiBandChart'
import { GalleryList } from '../components/GalleryList'
import { VerdictIcon } from '../components/VerdictIcon'
import { VERDICT_HEADLINE } from '../run/verdictCopy'
import { href, navigate } from '../router'
import type { RunCard, RunView } from '../types/api'

const STEPS = [
  {
    title: 'Read',
    body: 'Enter an arXiv ID: Nemotron models read the paper and pick the claims a run can test.',
    chip: 'Reader · Nemotron Super',
  },
  {
    title: 'Re-run',
    body: 'The experiment is planned, coded, smoke-tested and run in an isolated Nebius Token Factory sandbox, without internet.',
    chip: 'Planner · Ultra · Coder · Super',
  },
  {
    title: 'Decide',
    body: 'A fixed statistical rule, not a model, decides the verdict; every run ships its limitations.',
    chip: 'Rule, not a model',
  },
]

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

  // The newest gallery run, in full: its verdict is the page's example of what a run returns.
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
      <section className="hero">
        <div className="hero-main">
          <span className="eyebrow">Reproducibility check for ML papers</span>
          <h1 className="hero-title">Do the numbers in this paper hold up?</h1>
          <p className="lead">
            Give it an arXiv ID. Paper2Proof extracts the paper&rsquo;s claims, re-runs the experiment in an isolated
            Nebius sandbox and returns a verdict decided by a fixed statistical rule, with a signed Proof Bundle anyone
            can verify.
          </p>

          <form className="card start-form" onSubmit={submit} noValidate aria-describedby={error ? ids.error : undefined}>
            <h2>Start a run</h2>
            <div className="form-row">
              <div className="field field-wide">
                <label htmlFor={ids.arxiv}>arXiv ID</label>
                <input
                  id={ids.arxiv}
                  className="mono"
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
              </div>
              <div className="field">
                <label htmlFor={ids.invite}>Invite code</label>
                <input
                  id={ids.invite}
                  name="invite_code"
                  type="password"
                  autoComplete="off"
                  value={invite}
                  onChange={(e) => setInvite(e.target.value)}
                />
              </div>
            </div>
            <p id={ids.help} className="muted small">
              arXiv IDs only (for example 2106.09685 or 2106.09685v2): no URLs, no PDF uploads. The invite code is
              needed once per browser session: live runs use real compute, so they are invite-only.
            </p>
            <label htmlFor={ids.hint}>Claim to focus on (optional)</label>
            <textarea
              id={ids.hint}
              name="claim_hint"
              rows={2}
              maxLength={500}
              value={hint}
              onChange={(e) => setHint(e.target.value)}
            />
            <button type="submit" className="primary" disabled={busy}>
              {busy ? 'Starting…' : 'Start the run'}
            </button>
            {error && (
              <p id={ids.error} role="alert" className="error">
                {error}
              </p>
            )}
            <p className="muted small">Runs are private to your browser session. The public gallery is curated by hand.</p>
          </form>
        </div>

        {latest && <LatestVerdict run={latest} />}
      </section>

      <section className="section" aria-labelledby="how-h">
        <h2 id="how-h" className="section-title">
          From paper to proof in three steps
        </h2>
        <ol className="steps-grid" aria-label="How it works">
          {STEPS.map((s, i) => (
            <li key={s.title} className="card step-card">
              <div className="step-card-head">
                <span className="step-number" aria-hidden="true">
                  {i + 1}
                </span>
                <h3>{s.title}</h3>
              </div>
              <p className="muted">{s.body}</p>
              <span className="chip mono">{s.chip}</span>
            </li>
          ))}
        </ol>
      </section>

      <section className="section" aria-labelledby="examples-h">
        <div className="section-head">
          <h2 id="examples-h" className="section-title">
            Recent verdicts
          </h2>
          <a href={href.gallery}>Open the gallery →</a>
        </div>
        {examples.length === 0 ? (
          <p className="muted">
            No examples yet. See the <a href={href.gallery}>gallery</a>.
          </p>
        ) : (
          <GalleryList cards={examples} />
        )}
      </section>
    </>
  )
}

/** The newest gallery verdict with its chart: what a run returns, before starting one. */
function LatestVerdict({ run }: { run: RunView }) {
  const first = run.verdicts[0]
  if (!first) return null
  const claim = run.claims.find((c) => c.claim.id === first.claim_id)?.claim ?? null
  const kind = first.verdict.kind
  return (
    <aside className={`card latest verdict-${kind.toLowerCase()}`} aria-labelledby="latest-h">
      <div className="latest-head">
        <span className="muted small">
          Latest verdict · arXiv <span className="mono">{run.arxiv_id}</span>
        </span>
        <span className={`verdict-badge verdict-badge-${kind.toLowerCase()}`}>
          <VerdictIcon kind={kind} />
          <span id="latest-h">{VERDICT_HEADLINE[kind]}</span>
        </span>
      </div>
      {claim && <p className="latest-claim">&ldquo;{claim.text}&rdquo;</p>}
      <CiBandChart claim={claim} verdict={first.verdict} />
      <p className="chips">
        <span className="chip">
          {first.verdict.n_seeds} seed{first.verdict.n_seeds === 1 ? '' : 's'}
        </span>
        <span className="chip">decided by rule · explained by a model</span>
      </p>
      <p>
        <a href={href.run(run.id)}>Open this run →</a>
      </p>
    </aside>
  )
}
