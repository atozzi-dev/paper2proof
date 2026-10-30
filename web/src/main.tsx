import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Self-hosted typefaces (OFL-1.1): the CSP allows fonts from our own origin only.
import '@fontsource-variable/inter/wght.css'
import '@fontsource-variable/space-grotesk/wght.css'
import '@fontsource-variable/jetbrains-mono/wght.css'
import './index.css'
import App from './App.tsx'
import { applyTheme, storedTheme } from './run/theme'

applyTheme(storedTheme()) // before the first paint: no flash of the other theme

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
