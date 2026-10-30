// Proof Bundle download (docs/specs/api-and-events.md): GET /api/runs/{id}/bundle answers
// 302 to a short presigned URL, or 200 with the archive when the store can't presign (dev).
import { ApiError, bundleUrl } from './client'

/** Browser side effects, kept apart so tests can replace them (jsdom can do neither). */
export const browser = {
  /** A navigation, not a fetch: the presigned URL is off-origin (CSP connect-src 'self'). */
  navigate(url: string): void {
    window.location.assign(url)
  },
  save(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.append(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  },
}

const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const DISPOSITION_NAME = /filename="([^"]*)"/

/** The served file name when it is a plain name, else the API's own naming. */
export function bundleFilename(runId: string, disposition: string | null): string {
  const name = disposition?.match(DISPOSITION_NAME)?.[1]
  return name && SAFE_NAME.test(name) && !name.includes('..') ? name : `proof-bundle-${runId}.tar.gz`
}

/** Download the bundle: a redirect is followed by navigating to the API route (the browser
 * then follows it to the presigned URL), a 200 body is saved under its file name. */
export async function downloadBundle(runId: string): Promise<void> {
  const url = bundleUrl(runId)
  const res = await fetch(url, { credentials: 'same-origin', redirect: 'manual' })
  if (res.type === 'opaqueredirect' || (res.status >= 300 && res.status < 400)) {
    browser.navigate(url)
    return
  }
  if (!res.ok) {
    throw new ApiError(
      res.status,
      res.status === 404
        ? 'The Proof Bundle is not available yet. Try again in a minute.'
        : `The Proof Bundle could not be downloaded (${res.status}).`,
    )
  }
  browser.save(await res.blob(), bundleFilename(runId, res.headers.get('Content-Disposition')))
}
