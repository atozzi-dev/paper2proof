import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { VERDICT_HEADLINE } from '../run/verdictCopy'
import type { Stats, VerdictKind } from '../types/api'

const KINDS: VerdictKind[] = ['REPRODUCED', 'REPRODUCED_AT_REDUCED_SCALE', 'NOT_REPRODUCED', 'INCONCLUSIVE']

const ROLES: { role: string; family: string; job: string }[] = [
  { role: 'Reader', family: 'Nemotron Super', job: 'Reads the paper and extracts the quantitative claims with their source (table, section).' },
  { role: 'Vision', family: 'Nemotron 3.5 Super VL', job: 'Reads tables and figures the text extraction cannot (PDF papers).' },
  { role: 'Planner', family: 'Nemotron Ultra', job: 'Turns the claims into an experiment plan: scale, seeds, packages, datasets, compute minutes.' },
  { role: 'Coder', family: 'Nemotron Super', job: 'Writes and fixes the experiment code, smoke-tested in an isolated sandbox with no network.' },
  { role: 'Judge', family: 'Nemotron Ultra', job: 'Explains the verdict in plain words. It cannot change the verdict.' },
  { role: 'Utility', family: 'Nemotron Nano', job: 'Small classification and summarization tasks.' },
]

export function AboutPage() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    api.stats().then(setStats, () => setFailed(true))
  }, [])

  return (
    <>
      <h1>About Paper2Proof</h1>
      <p className="lead">
        Paper2Proof checks whether the numbers in an ML paper hold up. It re-runs the experiment in an isolated
        Nebius Token Factory sandbox and decides with a fixed statistical rule, not with a language model.
      </p>

      <section aria-labelledby="stats-h">
        <h2 id="stats-h">Numbers so far</h2>
        {failed && <p className="muted">The statistics are not available right now.</p>}
        {stats && (
          <table>
            <caption>Verdicts across all runs</caption>
            <thead>
              <tr>
                <th scope="col">Verdict</th>
                <th scope="col">Claims</th>
              </tr>
            </thead>
            <tbody>
              {KINDS.map((k) => (
                <tr key={k}>
                  <th scope="row">{VERDICT_HEADLINE[k]}</th>
                  <td>{stats.runs_by_verdict[k] ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="muted">
          A false accusation (a true claim reported as not reproduced) is the error we guard against most:
          &ldquo;not reproduced&rdquo; is only possible at full scale, when the whole confidence interval falls
          outside the tolerance band.
        </p>
      </section>

      <section aria-labelledby="arch-h">
        <h2 id="arch-h">How it works</h2>
        <ArchitectureDiagram />
        <ol className="steps-list">
          <li>The paper is fetched from arXiv by ID and its claims are extracted.</li>
          <li>The planner proposes an experiment and its cost; expensive plans wait for your approval.</li>
          <li>
            Packages and data are fetched by a locked-down container on our host: hash-pinned wheels and parquet
            files, stored in a per-run Nebius bucket and never executed there.
          </li>
          <li>The generated code is smoke-tested in a Nebius Token Factory sandbox with no network interface.</li>
          <li>
            The experiment runs in a fresh sandbox, also with no network, over several seeds (CPU only: the hardware is
            listed with every verdict).
          </li>
          <li>A pure function compares the measurements with the claim and returns the verdict.</li>
          <li>Everything is packed into a signed Proof Bundle that anyone can verify.</li>
        </ol>
      </section>

      <section aria-labelledby="models-h">
        <h2 id="models-h">How it uses Nemotron on Token Factory</h2>
        <p>
          Every model call goes to Nebius Token Factory. Each role is mapped to a Nemotron model by configuration,
          resolved against the live model list at startup, and every call is checked against the run budget and
          recorded in a cost ledger: the cost receipt on each run page comes from it.
        </p>
        <table>
          <caption>Model roles</caption>
          <thead>
            <tr>
              <th scope="col">Role</th>
              <th scope="col">Model family</th>
              <th scope="col">What it does</th>
            </tr>
          </thead>
          <tbody>
            {ROLES.map((r) => (
              <tr key={r.role}>
                <th scope="row">{r.role}</th>
                <td>{r.family}</td>
                <td>{r.job}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted">
          Each run page names the exact model behind every role in its cost receipt. Model output never decides a
          verdict and is never run as a privileged command: package installs, jobs and
          network access are done by deterministic code after validation.
        </p>
      </section>

      <p className="muted small">
        Typefaces: Inter, Space Grotesk and JetBrains Mono, served from this site under the{' '}
        <a href="/fonts/LICENSES.txt">SIL Open Font License 1.1</a>.
      </p>
    </>
  )
}

function ArchitectureDiagram() {
  const box = (x: number, y: number, w: number, title: string, sub: string) => (
    <g>
      <rect className="arch-box" x={x} y={y} width={w} height={52} rx={6} />
      <text className="arch-title" x={x + w / 2} y={y + 22} textAnchor="middle">
        {title}
      </text>
      <text className="arch-sub" x={x + w / 2} y={y + 40} textAnchor="middle">
        {sub}
      </text>
    </g>
  )
  const arrow = (x1: number, y1: number, x2: number, y2: number) => (
    <line className="arch-arrow" x1={x1} y1={y1} x2={x2} y2={y2} markerEnd="url(#arch-head)" />
  )
  return (
    <figure className="arch">
      <svg
        viewBox="0 0 640 250"
        role="img"
        aria-label="Architecture: the browser talks to the API on a Nebius Serverless Endpoint, which runs the orchestrator and the verdict engine, calls Nemotron models on Token Factory, and launches three isolated stages in a separate project: provision on CPU with allowlisted egress, smoke test in an isolated CPU job without network, and execute on GPU without internet. Results and the signed Proof Bundle go to Object Storage."
      >
        <defs>
          <marker id="arch-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0 L10,5 L0,10 z" className="arch-head" />
          </marker>
        </defs>
        {box(10, 10, 130, 'Browser', 'this page, SSE')}
        {box(200, 10, 220, 'API + orchestrator', 'our host, behind TLS')}
        {box(480, 10, 150, 'Token Factory', 'Nemotron roles')}
        {arrow(140, 36, 198, 36)}
        {arrow(420, 36, 478, 36)}
        <rect className="arch-zone" x={10} y={100} width={620} height={80} rx={8} />
        <text className="arch-sub" x={20} y={116}>
          Isolated · no secrets · per-run bucket
        </text>
        {box(20, 122, 180, 'Provision', 'locked-down container')}
        {box(230, 122, 180, 'Smoke test', 'Token Factory sandbox')}
        {box(440, 122, 180, 'Execute', 'sandbox, no network')}
        {arrow(200, 148, 228, 148)}
        {arrow(410, 148, 438, 148)}
        {arrow(310, 62, 310, 120)}
        {box(200, 192, 220, 'Verdict + Proof Bundle', 'pure function · signed')}
        {arrow(530, 174, 420, 210)}
      </svg>
    </figure>
  )
}
