import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Self-hosted typefaces (OFL-1.1): the CSP allows fonts from our own origin only.
import '@fontsource-variable/newsreader/opsz.css'
import '@fontsource-variable/newsreader/opsz-italic.css'
import '@fontsource-variable/instrument-sans/wght.css'
import '@fontsource-variable/caveat/wght.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
