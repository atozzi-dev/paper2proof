import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'

afterEach(() => {
  delete document.documentElement.dataset.theme
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
})

describe('Theme switch', () => {
  it('follows the system by default and overrides it on request, remembered', () => {
    render(<App />)
    const group = screen.getByRole('group', { name: 'Theme' })
    expect(within(group).getByRole('button', { name: 'System' })).toHaveAttribute('aria-pressed', 'true')
    expect(document.documentElement.dataset.theme).toBeUndefined()

    fireEvent.click(within(group).getByRole('button', { name: 'Dark' }))
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem('p2p-theme')).toBe('dark')
    expect(within(group).getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(within(group).getByRole('button', { name: 'System' }))
    expect(document.documentElement.dataset.theme).toBeUndefined()
    expect(window.localStorage.getItem('p2p-theme')).toBeNull()
  })

  it('still switches when the browser blocks storage', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Light' }))
    expect(document.documentElement.dataset.theme).toBe('light')
  })
})
