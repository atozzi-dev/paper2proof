import { href } from '../router'
import { verdictSummaryText } from '../run/verdictCopy'
import type { RunCard } from '../types/api'

export function GalleryList({ cards }: { cards: readonly RunCard[] }) {
  return (
    <ul className="gallery">
      {cards.map((card) => (
        <li key={card.id} className="card gallery-card">
          <h3>arXiv {card.arxiv_id}</h3>
          <p>{card.verdict_summary ? verdictSummaryText(card.verdict_summary) : 'No verdict recorded'}</p>
          <p className="muted">
            <time dateTime={card.created_at}>{new Date(card.created_at).toLocaleDateString()}</time>
          </p>
          <p className="actions">
            <a className="button" href={href.run(card.id, true)} aria-label={`Replay the run on arXiv ${card.arxiv_id}`}>
              Replay
            </a>{' '}
            <a href={href.run(card.id)} aria-label={`Open the run on arXiv ${card.arxiv_id}`}>
              Open
            </a>
          </p>
        </li>
      ))}
    </ul>
  )
}
