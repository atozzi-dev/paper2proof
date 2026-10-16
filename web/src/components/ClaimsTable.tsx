import type { Claim } from '../types/api'
import { fmtNum } from '../run/verdictCopy'

function reported(c: Claim): string {
  if (c.claim_type === 'ABSOLUTE') {
    if (c.reported_value === null || c.reported_value === undefined) return 'not stated'
    const std = c.reported_std === null || c.reported_std === undefined ? '' : ` ± ${fmtNum(c.reported_std)}`
    return `${fmtNum(c.reported_value)}${std}`
  }
  const base = c.baseline_name ?? 'the baseline'
  if (c.claimed_delta === null || c.claimed_delta === undefined) return `better than ${base}`
  return `${c.claimed_delta >= 0 ? '+' : ''}${fmtNum(c.claimed_delta)} vs ${base}`
}

/** Claims as extracted from the paper (untrusted text, rendered as plain text). */
export function ClaimsTable({ claims, tested }: { claims: readonly Claim[]; tested: ReadonlySet<string> }) {
  if (claims.length === 0) return <p className="muted">No claims extracted yet.</p>
  return (
    <div className="table-wrap">
      <table>
        <caption>Claims extracted from the paper</caption>
        <thead>
          <tr>
            <th scope="col">Claim</th>
            <th scope="col">Type</th>
            <th scope="col">Metric</th>
            <th scope="col">Reported</th>
            <th scope="col">Dataset · setting</th>
            <th scope="col">Source</th>
            <th scope="col">Tested</th>
          </tr>
        </thead>
        <tbody>
          {claims.map((c) => (
            <tr key={c.id}>
              <td>{c.text}</td>
              <td>{c.claim_type === 'ABSOLUTE' ? 'Absolute' : 'Relative'}</td>
              <td>
                {c.metric} <span className="muted">({c.higher_is_better ? 'higher is better' : 'lower is better'})</span>
              </td>
              <td>{reported(c)}</td>
              <td>
                {c.dataset}
                {c.setting && ` · ${c.setting}`}
              </td>
              <td>{c.source_ref}</td>
              <td>{tested.has(c.id) ? 'Yes' : 'No'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
