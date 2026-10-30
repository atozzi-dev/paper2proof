import { useLayoutEffect, useRef, useState } from 'react'
import type { LogLine } from '../run/state'

export const ROW_PX = 18
const VIEW_PX = 288
const OVERSCAN = 20

/** Virtualized, auto-following log. Lines come from untrusted code: React text nodes only. */
export function LogPanel({ title, lines }: { title: string; lines: readonly LogLine[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const follow = useRef(true)

  useLayoutEffect(() => {
    const el = ref.current
    if (el && follow.current) {
      el.scrollTop = el.scrollHeight
      setScrollTop(el.scrollTop)
    }
  }, [lines.length])

  const total = lines.length * ROW_PX
  const first = Math.max(Math.floor(scrollTop / ROW_PX) - OVERSCAN, 0)
  const last = Math.min(first + Math.ceil(VIEW_PX / ROW_PX) + 2 * OVERSCAN, lines.length)
  const visible = lines.slice(first, last)

  return (
    <section className="log-panel">
      <h3>
        {title} <span className="muted">({lines.length} lines)</span>
      </h3>
      {lines.length === 0 ? (
        <p className="muted">No output yet.</p>
      ) : (
        <div
          ref={ref}
          className="log-view"
          role="log"
          aria-live="off"
          aria-label={title}
          tabIndex={0}
          style={{ height: Math.min(VIEW_PX, Math.max(lines.length, 4) * ROW_PX + 2) }}
          onScroll={(e) => {
            const el = e.currentTarget
            follow.current = el.scrollTop + el.clientHeight >= el.scrollHeight - ROW_PX
            setScrollTop(el.scrollTop)
          }}
        >
          <div style={{ height: total, position: 'relative' }}>
            <div style={{ transform: `translateY(${first * ROW_PX}px)` }}>
              {visible.map((line) => (
                <div key={line.key} className={line.stderr ? 'log-line stderr' : 'log-line'} style={{ height: ROW_PX }}>
                  <span className="log-source">{line.source}</span>
                  {line.text}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
