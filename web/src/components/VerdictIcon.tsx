import type { VerdictKind } from '../types/api'

/** A shape per verdict, so it never relies on colour alone: check, cross, or a dash. */
export function VerdictIcon({ kind, size = 14 }: { kind: VerdictKind; size?: number }) {
  return (
    <svg className="verdict-icon" width={size} height={size} viewBox="0 0 14 14" fill="none" aria-hidden="true">
      {kind === 'NOT_REPRODUCED' ? (
        <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" strokeWidth="2" strokeLinecap="round" />
      ) : kind === 'INCONCLUSIVE' ? (
        <path d="M3 7h8" strokeWidth="2" strokeLinecap="round" />
      ) : (
        <path d="M2.5 7.5l3 3 6-7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      )}
    </svg>
  )
}
