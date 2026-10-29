import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { GalleryList } from '../components/GalleryList'
import type { RunCard } from '../types/api'

export function GalleryPage() {
  const [cards, setCards] = useState<RunCard[] | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    api.gallery().then(setCards, () => setError(true))
  }, [])

  return (
    <section>
      <h1>Gallery</h1>
      <p className="lead">
        Recorded runs, chosen by hand. A replay re-streams the recorded events at 10× speed: no experiment runs again
        and nothing is executed.
      </p>
      {error && (
        <p role="alert" className="error">
          The gallery could not be loaded. Try again later.
        </p>
      )}
      {cards === null && !error && <p className="muted">Loading…</p>}
      {cards?.length === 0 && <p className="muted">The gallery is empty for now.</p>}
      {cards && cards.length > 0 && <GalleryList cards={cards} />}
    </section>
  )
}
