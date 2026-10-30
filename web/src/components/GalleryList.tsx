import { href } from '../router'
import { verdictSummaryText } from '../run/verdictCopy'
import type { RunCard, VerdictKind } from '../types/api'
import { VerdictIcon } from './VerdictIcon'

/** The card's badge: the most cautionary verdict in the summary ("1 REPRODUCED, 1 NOT_REPRODUCED"). */
function badgeKind(summary: string | null): VerdictKind | null {
  if (!summary) return null
  const order: VerdictKind[] = ['NOT_REPRODUCED', 'INCONCLUSIVE', 'REPRODUCED_AT_REDUCED_SCALE', 'REPRODUCED']
  return order.find((k) => new RegExp(`\\b${k}\\b`).test(summary)) ?? null
}

export function GalleryList({ cards }: { cards: readonly RunCard[] }) {
  return (
    <ul className="gallery">
      {cards.map((card) => {
        const kind = badgeKind(card.verdict_summary)
        return (
          <li key={card.id} className="card gallery-card">
            <div className="gallery-card-head">
              {kind && (
                <span className={`verdict-badge verdict-badge-${kind.toLowerCase()}`} aria-hidden="true">
                  <VerdictIcon kind={kind} />
                </span>
              )}
              <h3>
                arXiv <span className="mono">{card.arxiv_id}</span>
              </h3>
            </div>
            <p className="gallery-summary">
              {card.verdict_summary ? verdictSummaryText(card.verdict_summary) : 'No verdict recorded'}
            </p>
            {card.verdict_summary?.includes('NOT_REPRODUCED') && (
              // Invariant 10: "not reproduced" always comes with the conditions it was reached under.
              <p className="muted small">
                <a href={href.run(card.id)}>Conditions: scale, seeds and hardware</a>
              </p>
            )}
            <p className="muted small">
              <time dateTime={card.created_at}>{new Date(card.created_at).toLocaleDateString()}</time>
            </p>
            <p className="actions">
              <a className="button dark" href={href.run(card.id, true)} aria-label={`Replay the run on arXiv ${card.arxiv_id}`}>
                ▶ Replay
              </a>
              <a className="button ghost" href={href.run(card.id)} aria-label={`Open the run on arXiv ${card.arxiv_id}`}>
                Open
              </a>
            </p>
          </li>
        )
      })}
    </ul>
  )
}
