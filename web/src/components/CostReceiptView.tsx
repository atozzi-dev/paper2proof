import type { CostUpdate, ModelCall } from '../types/api'
import { costReceipt, formatUsd } from '../run/cost'

const n = (x: number) => x.toLocaleString('en-US')

export function CostReceiptView({ calls, update }: { calls: readonly ModelCall[]; update: CostUpdate | null }) {
  const r = costReceipt(calls, update)
  return (
    <table className="receipt">
      <caption>Cost receipt</caption>
      <thead>
        <tr>
          <th scope="col">Role</th>
          <th scope="col">Calls</th>
          <th scope="col">Tokens in</th>
          <th scope="col">Tokens out</th>
          <th scope="col">Cost</th>
        </tr>
      </thead>
      <tbody>
        {r.rows.map((row) => (
          <tr key={row.role}>
            <th scope="row">{row.role}</th>
            <td>{row.calls}</td>
            <td>{n(row.tokensIn)}</td>
            <td>{n(row.tokensOut)}</td>
            <td>
              {formatUsd(row.usd)}
              {row.unpriced > 0 && ' *'}
            </td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <th scope="row">Inference</th>
          <td>{r.rows.reduce((s, x) => s + x.calls, 0)}</td>
          <td>{n(r.tokensIn)}</td>
          <td>{n(r.tokensOut)}</td>
          <td>{formatUsd(r.inferenceUsd)}</td>
        </tr>
        <tr>
          <th scope="row">Cloud jobs</th>
          <td colSpan={3} />
          <td>{r.cloudReported ? formatUsd(r.cloudUsd) : <span className="muted">not reported yet</span>}</td>
        </tr>
        <tr>
          <th scope="row">Total</th>
          <td colSpan={3} />
          <td>
            <strong>{formatUsd(r.totalUsd)}</strong>
            {!r.cloudReported && <span className="muted"> + cloud not reported yet</span>}
          </td>
        </tr>
        {r.unpricedCalls > 0 && (
          <tr>
            <td colSpan={5} className="muted">
              * {r.unpricedCalls} call(s) used a model without a configured price and are not included.
            </td>
          </tr>
        )}
      </tfoot>
    </table>
  )
}
