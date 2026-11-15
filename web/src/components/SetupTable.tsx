import type { SetupRecord } from '../types/api'

/** The conditions the paper states, against what the run used (app/orchestrator/setup_check.py):
 * a number counts only when it is written in the code; a choice is as the Coder declares it. */
export function SetupTable({ setup }: { setup: readonly SetupRecord[] }) {
  if (setup.length === 0) {
    return <p className="muted">The setup is recorded once the results are analyzed.</p>
  }
  return (
    <div className="table-wrap">
      <table className="setup-table">
        <caption>The paper&rsquo;s setup</caption>
        <thead>
          <tr>
            <th scope="col">Condition</th>
            <th scope="col">The paper</th>
            <th scope="col">This run</th>
            <th scope="col">How we know</th>
          </tr>
        </thead>
        <tbody>
          {setup.map((s) => (
            <tr key={s.id} className={s.matches_paper ? undefined : 'differs'}>
              <th scope="row">{s.name}</th>
              <td className="mono">{s.paper}</td>
              <td className="mono">
                {s.used}
                {!s.matches_paper && <span className="differs-tag"> differs</span>}
              </td>
              <td>
                {s.kind === 'NUMBER' ? (
                  <>
                    found in <span className="mono">{s.where}</span>
                  </>
                ) : (
                  'declared by the Coder'
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
