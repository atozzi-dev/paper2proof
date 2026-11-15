import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'

afterEach(() => {
  window.localStorage.clear()
  vi.restoreAllMocks()
})

describe('App', () => {
  it('names the product in the header and asks the question on the start page', () => {
    window.location.hash = '#/'
    render(<App />)
    expect(screen.getByRole('link', { name: 'Paper2Proof home' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Do the numbers in this paper hold up?')
  })

  it('shows the masthead: name, tagline and the two sections', () => {
    window.location.hash = '#/gallery'
    render(<App />)
    expect(screen.getByRole('link', { name: 'Paper2Proof home' })).toHaveTextContent(
      'Reproducibility reports for ML papers',
    )
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual(['Gallery', 'About'])
    expect(within(nav).getByRole('link', { name: 'Gallery' })).toHaveAttribute('aria-current', 'page')
  })

  it('keeps Gallery current on a run page', () => {
    window.location.hash = `#/runs/r_${'a'.repeat(26)}`
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(() => {}))
    render(<App />)
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getByRole('link', { name: 'Gallery' })).toHaveAttribute('aria-current', 'page')
  })

  it('has a single light theme: no theme switch', () => {
    window.localStorage.setItem('p2p-theme', 'dark') // a choice saved by an earlier version
    window.location.hash = '#/'
    render(<App />)
    expect(screen.queryByRole('group', { name: 'Theme' })).toBeNull()
    expect(document.documentElement.dataset.theme).toBeUndefined()
  })
})
