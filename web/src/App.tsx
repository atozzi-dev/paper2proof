import { useEffect } from 'react'
import { ThemeSwitch } from './components/ThemeSwitch'
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

/** Brackets around a check mark: a claim, checked. */
function Logo() {
  return (
    <svg className="logo" width="30" height="30" viewBox="0 0 30 30" fill="none" aria-hidden="true">
      <path className="logo-brackets" d="M9 4H5v22h4M21 4h4v22h-4" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      <path className="logo-check" d="M10 15.5l3.5 3.5L20.5 11" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function App() {
  const route = useRoute()

  useEffect(() => {
    document.title = TITLES[route.page]
    window.scrollTo?.(0, 0)
  }, [route])

  const current = (page: Route['page']) => (route.page === page || (page === 'gallery' && route.page === 'run') ? 'page' : undefined)

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="site-header">
        <div className="container site-header-inner">
          <a className="brand" href={href.home} aria-label="Paper2Proof home">
            <Logo />
            <span>Paper2Proof</span>
          </a>
          <nav aria-label="Main">
            <a href={href.home} aria-current={current('home')}>
              Home
            </a>
            <a href={href.gallery} aria-current={current('gallery')}>
              Gallery
            </a>
            <a href={href.about} aria-current={current('about')}>
              About
            </a>
          </nav>
          <ThemeSwitch />
        </div>
      </header>
      <main id="main" className="container" tabIndex={-1}>
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
        <div className="container site-footer-inner">
          <p>Runs on Nebius Token Factory and AI Cloud with NVIDIA Nemotron models · Apache-2.0</p>
          <p>
            &ldquo;Not reproduced&rdquo; always means not reproduced under our conditions, listed next to every
            verdict.
          </p>
        </div>
      </footer>
    </>
  )
}

export default App
