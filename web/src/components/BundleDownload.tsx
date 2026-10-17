import { useState } from 'react'
import { ApiError, bundleUrl } from '../api/client'
import { downloadBundle } from '../api/download'
import type { Warning } from '../types/api'

/** The Proof Bundle download, with the BUNDLE_FAILED / BUNDLE_UNSIGNED warnings next to it. */
export function BundleDownload({ runId, warnings }: { runId: string; warnings: readonly Warning[] }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const failed = warnings.some((w) => w.code === 'BUNDLE_FAILED')
  const unsigned = warnings.some((w) => w.code === 'BUNDLE_UNSIGNED')

  if (failed) {
    return (
      <p role="note" className="warning">
        The Proof Bundle could not be built for this run; the verdict above stands.
      </p>
    )
  }

  async function onClick(e: React.MouseEvent<HTMLAnchorElement>) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await downloadBundle(runId)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The Proof Bundle could not be downloaded.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {unsigned && (
        <p role="note" className="warning">
          This Proof Bundle is not signed (the signing service was unavailable), so it can't be checked against the
          published key.
        </p>
      )}
      <a className="button" href={bundleUrl(runId)} download onClick={onClick} aria-disabled={busy}>
        Download the Proof Bundle
      </a>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </>
  )
}
