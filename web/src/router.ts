// Hash routing: deep links work without any server-side SPA fallback.
import { useEffect, useState } from 'react'

export type Route =
  | { page: 'home' }
  | { page: 'run'; id: string; replay: boolean }
  | { page: 'gallery' }
  | { page: 'about' }
  | { page: 'not_found' }

export function parseRoute(hash: string): Route {
  const [path, query = ''] = hash.replace(/^#/, '').split('?', 2)
  const parts = path.split('/').filter(Boolean)
  if (parts.length === 0) return { page: 'home' }
  if (parts.length === 1 && parts[0] === 'gallery') return { page: 'gallery' }
  if (parts.length === 1 && parts[0] === 'about') return { page: 'about' }
  if (parts.length === 2 && parts[0] === 'runs') {
    let id: string
    try {
      id = decodeURIComponent(parts[1])
    } catch {
      return { page: 'not_found' } // malformed escape in a shared link
    }
    return { page: 'run', id, replay: new URLSearchParams(query).get('replay') === '1' }
  }
  return { page: 'not_found' }
}

export const href = {
  home: '#/',
  gallery: '#/gallery',
  about: '#/about',
  run: (id: string, replay = false) => `#/runs/${encodeURIComponent(id)}${replay ? '?replay=1' : ''}`,
}

export function navigate(to: string): void {
  window.location.hash = to
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash))
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}
