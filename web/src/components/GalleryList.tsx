import { href } from '../router'
import { fmtDate } from '../run/format'
import { parseSummary } from '../run/summary'
import type { RunCard } from '../types/api'
import { VerdictPill } from './VerdictPill'

/** Report cards: the paper, its verdicts (most cautionary first), a replay and the report. */
export function GalleryList({ cards }: { cards: readonly RunCard[] }) {
  return (
    <ul className="report-cards">
      {cards.map((card) => {
        const { counts, more } = parseSummary(card.verdict_summary)
        // The server folds the tail of a long summary into "+n more" (app/verdict/summary.py): a
        // NOT_REPRODUCED may be in it, so the conditions link stays whenever something is folded.
        const notReproduced = counts.some((c) => c.kind === 'NOT_REPRODUCED') || more > 0
        return (
          <li key={card.id} className="report-card">
            <p className="report-card-meta">
              Report of <time dateTime={card.created_at}>{fmtDate(card.created_at)}</time>
            </p>
            <h3>
              <a href={href.run(card.id)}>arXiv {card.arxiv_id}</a>
            </h3>
            <p className="report-card-verdicts">
              {counts.length === 0 && <span className="muted">No verdict recorded</span>}
              {counts.map((c) => (
                <VerdictPill key={c.kind} kind={c.kind} count={c.count} size="sm" />
              ))}
              {more > 0 && <span className="pill pill-sm pill-more">+{more} more</span>}
            </p>
            {notReproduced && (
              // Invariant 10: "not reproduced" always comes with the conditions it was reached under.
              <p className="small">
                <a href={href.run(card.id)}>Conditions: scale, seeds and hardware</a>
              </p>
            )}
            <p className="report-card-actions">
              <a href={href.run(card.id, true)} aria-label={`Replay the run on arXiv ${card.arxiv_id}`}>
                ▶ Replay
              </a>
              <a href={href.run(card.id)} aria-label={`Read the report on arXiv ${card.arxiv_id}`} className="strong">
                Read
              </a>
            </p>
          </li>
        )
      })}
    </ul>
  )
}
