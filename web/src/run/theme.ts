export type ThemeChoice = 'system' | 'light' | 'dark'

export const THEME_KEY = 'p2p-theme'

/** The saved choice; System when nothing is saved or storage is unavailable (private mode). */
export function storedTheme(): ThemeChoice {
  try {
    const saved = window.localStorage.getItem(THEME_KEY)
    return saved === 'light' || saved === 'dark' ? saved : 'system'
  } catch {
    return 'system'
  }
}

/** System follows prefers-color-scheme (no attribute); Light and Dark override it. */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement
  if (choice === 'system') delete root.dataset.theme
  else root.dataset.theme = choice
}

/** Remember the choice; when storage is unavailable it lasts for this page only. */
export function saveTheme(choice: ThemeChoice): void {
  try {
    if (choice === 'system') window.localStorage.removeItem(THEME_KEY)
    else window.localStorage.setItem(THEME_KEY, choice)
  } catch {
    // private mode or blocked storage
  }
}
