import { useState } from 'react'
import { applyTheme, saveTheme, storedTheme, type ThemeChoice } from '../run/theme'

const CHOICES: { value: ThemeChoice; label: string }[] = [
  { value: 'system', label: 'System' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

export function ThemeSwitch() {
  const [choice, setChoice] = useState<ThemeChoice>(storedTheme)

  function pick(next: ThemeChoice) {
    setChoice(next)
    applyTheme(next)
    saveTheme(next)
  }

  return (
    <div className="theme-switch" role="group" aria-label="Theme">
      {CHOICES.map((c) => (
        <button key={c.value} type="button" aria-pressed={choice === c.value} onClick={() => pick(c.value)}>
          {c.label}
        </button>
      ))}
    </div>
  )
}
