import { splitHighlight } from '../run/highlight'
import type { Claim } from '../types/api'

/** The claim in the paper's words (untrusted: plain text only), its value highlighted. */
export function ClaimQuote({ claim, size = 'lg', text }: { claim: Claim; size?: 'lg' | 'md' | 'sm'; text?: string }) {
  const parts = splitHighlight(text ?? claim.text, claim)
  return (
    <blockquote className={`claim-quote claim-quote-${size}`}>
      &ldquo;
      {parts.map((p, i) =>
        p.mark ? (
          <mark key={i} className="mark">
            {p.text}
          </mark>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
      &rdquo;
    </blockquote>
  )
}
