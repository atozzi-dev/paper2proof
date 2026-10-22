import { useEffect, useId, useState, type FormEvent } from 'react'
import { ARXIV_ID, ApiError, api } from '../api/client'
import { GalleryList } from '../components/GalleryList'
import { href, navigate } from '../router'
import type { RunCard } from '../types/api'

export function HomePage() {
  const ids = { arxiv: useId(), invite: useId(), hint: useId(), help: useId(), error: useId() }
  const [arxivId, setArxivId] = useState('')
  const [invite, setInvite] = useState('')
  const [hint, setHint] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [examples, setExamples] = useState<RunCard[]>([])

  useEffect(() => {
    api.gallery().then((cards) => setExamples(cards.slice(0, 4)), () => setExamples([]))
  }, [])

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
        <h1>Paper2Proof</h1>
        <p className="lead">
          Give it an arXiv ML paper. It extracts the quantitative claims, re-runs the experiment in an isolated GPU
          sandbox, and returns a deterministic verdict with a signed, re-runnable Proof Bundle.
        </p>
        <ol className="steps-list" aria-label="How it works">
          <li>Enter an arXiv ID: Nemotron models read the paper and pick the claims a run can test.</li>
          <li>The experiment is planned, coded, smoke-tested and run on Nebius GPUs, isolated and without internet.</li>
          <li>A fixed statistical rule, not a model, decides the verdict; every run ships its limitations.</li>
        </ol>
        {examples.length > 0 && (
          <p>
            <a className="button" href={href.run(examples[0].id, true)}>
              Watch a finished run in 30 seconds
            </a>{' '}
            <span className="muted">a recorded run, replayed at 10× speed: nothing is executed</span>
          </p>
        )}
      </section>

      <form className="card start-form" onSubmit={submit} noValidate aria-describedby={error ? ids.error : undefined}>
        <h2>Start a run</h2>
        <label htmlFor={ids.arxiv}>arXiv ID</label>
        <input
          id={ids.arxiv}
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
        <p id={ids.help} className="muted">
          arXiv IDs only (for example 2106.09685 or 2106.09685v2): no URLs, no PDF uploads.
        </p>
        <label htmlFor={ids.invite}>Invite code</label>
        <input
          id={ids.invite}
          name="invite_code"
          type="password"
          autoComplete="off"
          value={invite}
          onChange={(e) => setInvite(e.target.value)}
        />
        <p className="muted">Needed once per browser session. Live runs use real GPUs, so they are invite-only.</p>
        <label htmlFor={ids.hint}>Claim to focus on (optional)</label>
        <textarea
          id={ids.hint}
          name="claim_hint"
          rows={2}
          maxLength={500}
          value={hint}
          onChange={(e) => setHint(e.target.value)}
        />
        <button type="submit" disabled={busy}>
          {busy ? 'Starting…' : 'Start the run'}
        </button>
        {error && (
          <p id={ids.error} role="alert" className="error">
            {error}
          </p>
        )}
        <p className="muted">Runs are private to your browser session. The public gallery is curated by hand.</p>
      </form>

      <section aria-labelledby="examples-h">
        <h2 id="examples-h">Examples from the gallery</h2>
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
