import { useEffect } from 'react'
import { AboutPage } from './pages/About'
import { GalleryPage } from './pages/Gallery'
import { HomePage } from './pages/Home'
import { RunPage } from './pages/Run'
import { href, useRoute, type Route } from './router'

const TITLES: Record<Route['page'], string> = {
  home: 'Paper2Proof',
  run: 'Run · Paper2Proof',
  gallery: 'Gallery · Paper2Proof',
  about: 'About · Paper2Proof',
  not_found: 'Not found · Paper2Proof',
}

function App() {
  const route = useRoute()

  useEffect(() => {
    document.title = TITLES[route.page]
    window.scrollTo?.(0, 0)
  }, [route])

  const current = (page: Route['page']) => (route.page === page ? 'page' : undefined)

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="site-header">
        <a className="brand" href={href.home}>
          Paper2Proof
        </a>
        <nav aria-label="Main">
          <a href={href.home} aria-current={current('home')}>
            Start
          </a>
          <a href={href.gallery} aria-current={current('gallery')}>
            Gallery
          </a>
          <a href={href.about} aria-current={current('about')}>
            About
          </a>
        </nav>
      </header>
      <main id="main" tabIndex={-1}>
        {route.page === 'home' && <HomePage />}
        {route.page === 'run' && <RunPage id={route.id} replay={route.replay} />}
        {route.page === 'gallery' && <GalleryPage />}
        {route.page === 'about' && <AboutPage />}
        {route.page === 'not_found' && (
          <section>
            <h1>Page not found</h1>
            <p>
              <a href={href.home}>Go to the start page</a>
            </p>
          </section>
        )}
      </main>
      <footer className="site-footer">
        <p>
          Paper2Proof · Apache-2.0 · Built on Nebius AI Cloud and NVIDIA Nemotron. &ldquo;Not reproduced&rdquo;
          always means not reproduced under our conditions, which are listed next to every verdict.
        </p>
      </footer>
    </>
  )
}

export default App
