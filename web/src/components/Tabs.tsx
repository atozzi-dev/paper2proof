import { useId, useRef, type KeyboardEvent, type ReactNode } from 'react'

export interface TabSpec<T extends string> {
  id: T
  label: string
}

interface Props<T extends string> {
  label: string
  tabs: readonly TabSpec<T>[]
  selected: T
  onSelect: (id: T) => void
  /** The selected tab's panel: only that one is rendered. */
  children: ReactNode
}

/** ARIA tabs: the selected tab is the only one in the tab order; arrows, Home and End move
 * between tabs (and wrap around), selecting as they go. */
export function Tabs<T extends string>({ label, tabs, selected, onSelect, children }: Props<T>) {
  const base = useId()
  const refs = useRef(new Map<T, HTMLButtonElement>())
  const tabId = (id: T) => `${base}-tab-${id}`
  const panelId = `${base}-panel`

  function move(e: KeyboardEvent<HTMLButtonElement>) {
    const i = tabs.findIndex((t) => t.id === selected)
    const next =
      e.key === 'ArrowRight'
        ? (i + 1) % tabs.length
        : e.key === 'ArrowLeft'
          ? (i - 1 + tabs.length) % tabs.length
          : e.key === 'Home'
            ? 0
            : e.key === 'End'
              ? tabs.length - 1
              : null
    if (next === null) return
    e.preventDefault()
    const id = tabs[next].id
    onSelect(id)
    refs.current.get(id)?.focus()
  }

  return (
    <>
      <div role="tablist" aria-label={label} className="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            ref={(el) => {
              if (el) refs.current.set(t.id, el)
              else refs.current.delete(t.id)
            }}
            id={tabId(t.id)}
            type="button"
            role="tab"
            className="tab"
            aria-selected={t.id === selected}
            aria-controls={panelId}
            tabIndex={t.id === selected ? 0 : -1}
            onClick={() => onSelect(t.id)}
            onKeyDown={move}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={panelId} aria-labelledby={tabId(selected)} className="tabpanel" tabIndex={0}>
        {children}
      </div>
    </>
  )
}
