import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(cleanup)

// jsdom has no scrolling; the app scrolls to the top on navigation.
if (typeof window !== 'undefined') window.scrollTo = () => {}
