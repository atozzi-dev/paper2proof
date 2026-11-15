import type { VerdictKind } from '../types/api'
import { pillText } from '../run/summary'
import { VerdictIcon } from './VerdictIcon'

interface Props {
  kind: VerdictKind
  count?: number
  size?: 'sm' | 'md' | 'lg'
  /** A verdict's own heading, or a label inside a sentence or a card. */
  as?: 'span' | 'h2' | 'h3'
  id?: string
}

/** A verdict in its mandated words, with a shape (check, cross, dash) and its colour. */
export function VerdictPill({ kind, count, size = 'md', as: Tag = 'span', id }: Props) {
  const sizeClass = size === 'md' ? '' : ` pill-${size}`
  return (
    <Tag id={id} className={`pill pill-${kind.toLowerCase()}${sizeClass}`}>
      <VerdictIcon kind={kind} size={size === 'sm' ? 11 : 14} />
      {pillText(kind, count)}
    </Tag>
  )
}
