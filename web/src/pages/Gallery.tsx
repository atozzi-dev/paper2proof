import { useEffect, useId, useState } from 'react'
import { api } from '../api/client'
import { GalleryList } from '../components/GalleryList'
import { CAUTION_ORDER, parseSummary } from '../run/summary'
import { VERDICT_HEADLINE } from '../run/verdictCopy'
import type { RunCard, VerdictKind } from '../types/api'

const FILTER_LABEL: Record<VerdictKind, string> = {
  ...VERDICT_HEADLINE, // "Not reproduced under our conditions" stays whole, even on a button
  REPRODUCED_AT_REDUCED_SCALE: 'Reduced scale',
}

export function GalleryPage() {
  const searchId = useId()
  const [cards, setCards] = useState<RunCard[] | null>(null)
  const [error, setError] = useState(false)
  const [kind, setKind] = useState<VerdictKind | null>(null)
  const [query, setQuery] = useState('')

  useEffect(() => {
    api.gallery().then(setCards, () => setError(true))
  }, [])

  const kindsOf = (card: RunCard) => new Set(parseSummary(card.verdict_summary).counts.map((c) => c.kind))
  const q = query.trim().toLowerCase()
  const words = q.split(/\s+/).filter(Boolean)
  // an arXiv ID, or words of the title in any order
  const matches = (c: RunCard) =>
    q === '' ||
    c.arxiv_id.toLowerCase().includes(q) ||
    (c.title != null && words.every((w) => c.title?.toLowerCase().includes(w)))
  const shown = (cards ?? []).filter((c) => (kind === null || kindsOf(c).has(kind)) && matches(c))

  return (
    <>
      <header className="page-header">
        <div className="page-header-main">
          <h1 className="page-title">Gallery</h1>
          <p className="lead">
            Reports picked by hand. Replay any run at 10× speed: the recorded events are streamed again, nothing is
            executed, and every verdict keeps the conditions it was reached under.
          </p>
        </div>
        <div className="search-field">
          <label htmlFor={searchId}>Find a paper</label>
          <input
            id={searchId}
            type="search"
            placeholder="arXiv ID or words from the title"
            autoComplete="off"
            spellCheck={false}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      </header>

      {error && (
        <p role="alert" className="error">
          The gallery could not be loaded. Try again later.
        </p>
      )}
      {cards === null && !error && <p className="muted">Loading…</p>}
      {cards?.length === 0 && <p className="muted">The gallery is empty for now.</p>}
      {cards && cards.length > 0 && (
        <>
          <div role="group" aria-label="Show reports" className="filters">
            <button type="button" aria-pressed={kind === null} onClick={() => setKind(null)}>
              All {cards.length} paper{cards.length === 1 ? '' : 's'}
            </button>
            {CAUTION_ORDER.map((k) => (
              <button key={k} type="button" aria-pressed={kind === k} onClick={() => setKind(k)}>
                {FILTER_LABEL[k]} · {cards.filter((c) => kindsOf(c).has(k)).length}
              </button>
            ))}
          </div>
          {shown.length === 0 ? <p className="muted">No report matches.</p> : <GalleryList cards={shown} />}
        </>
      )}
    </>
  )
}
