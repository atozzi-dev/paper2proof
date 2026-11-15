import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { VerdictPill } from '../components/VerdictPill'
import { CAUTION_ORDER } from '../run/summary'
import { VERDICT_HEADLINE } from '../run/verdictCopy'
import type { Stats, VerdictKind } from '../types/api'

const ROLES: { role: string; family: string; job: string }[] = [
  { role: 'Reader', family: 'Nemotron 3.5 Super VL', job: 'Reads the paper and extracts the quantitative claims with their source (table, section).' },
  { role: 'Vision', family: 'Nemotron 3.5 Super VL', job: 'Reads tables and figures the text extraction cannot (PDF papers).' },
  { role: 'Planner', family: 'Nemotron Ultra', job: "Turns the claims into an experiment plan: scale, seeds, packages, datasets, compute minutes and the paper's setup to check." },
  { role: 'Coder', family: 'Nemotron 3.5 Super VL', job: 'Writes and fixes the experiment code, smoke-tested in an isolated sandbox with no network.' },
  { role: 'Judge', family: 'Nemotron Ultra', job: 'Explains the verdict in plain words. It cannot change the verdict.' },
  { role: 'Utility', family: 'Nemotron 3.5 Lightning', job: 'Small classification and summarization tasks.' },
]

// docs/specs/verdict-engine.md and config/verdict.yaml: what each verdict means, as the engine applies it.
const RULES: { kind: VerdictKind; text: string }[] = [
  {
    kind: 'REPRODUCED',
    text: "At full scale. An absolute claim: our mean is within the tolerance of the paper’s value, or better. A claim that A beats B: the whole 95% interval of the per-seed difference is on A’s side of zero, and the mean gain is at least half the claimed one.",
  },
  {
    kind: 'NOT_REPRODUCED',
    text: "Only at full scale, on the paper’s setup. An absolute claim: the whole 95% interval falls short of the paper’s value by more than the tolerance. A claim that A beats B: the whole interval is on B’s side of zero (or at it), and our baseline reached the paper’s own.",
  },
  {
    kind: 'INCONCLUSIVE',
    text: "The interval straddles the threshold; fewer than 3 seeds gave a value; the experiment failed or the claim states nothing testable; an absolute value was measured at reduced scale; our baseline fell short of the paper’s; or a shortfall is so large (more than twice off) that it points to a broken run rather than a wrong paper.",
  },
  {
    kind: 'REPRODUCED_AT_REDUCED_SCALE',
    text: "The run had to be smaller than the paper, or differ from the setup it states. At reduced scale a run can confirm a claimed gain, never accuse a paper; an absolute value stays inconclusive.",
  },
]

const STEPS: { title: string; body: string }[] = [
  { title: 'Read', body: 'The paper is fetched from arXiv by ID; the claims are extracted with the place they come from.' },
  { title: 'Plan', body: 'Scale, seeds, cost and the conditions of the paper’s setup. A plan estimated above $0.60 waits for your approval.' },
  { title: 'Provision', body: 'Hash-pinned wheels and data, fetched by a locked-down container into a per-run bucket and never executed there.' },
  { title: 'Smoke test', body: 'The Coder’s script runs in a sandbox with no network, with up to 8 fixes.' },
  { title: 'Execute', body: 'A fresh sandbox, also without network, runs the experiment over several seeds, on CPU.' },
  { title: 'Decide and sign', body: 'A pure function decides; every file of the run goes into the signed Proof Bundle.' },
]

export function AboutPage() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    api.stats().then(setStats, () => setFailed(true))
  }, [])

  return (
    <div className="about">
      <header className="about-header">
        <div className="page-header-main">
          <p className="kicker">About</p>
          <h1 className="page-title">How we check a paper</h1>
          <p className="lead">
            Paper2Proof re-runs the experiment behind an ML paper&rsquo;s claims and tells you, claim by claim, whether
            the numbers hold, and under which conditions.
          </p>
        </div>
        <p className="about-motto">
          A rule decides.
          <br />
          <em>A model only explains.</em>
        </p>
      </header>

      <section className="about-section" aria-labelledby="short-h">
        <h2 id="short-h">The short version</h2>
        <div className="about-columns">
          <p className="about-text">
            You give it an arXiv ID, nothing else. A model reads the paper and picks the claims a run can test. The
            experiment is written from the paper, tried in a sandbox with no network, and run over at least three seeds.
            Then a pure function compares our measurements with the paper&rsquo;s value, and that settles the verdict.
            Every file of the run is hashed and signed, so anyone can check it and run it again.
          </p>
          <dl className="facts about-facts">
            <dt>Input</dt>
            <dd>an arXiv ID: no URLs, no PDF uploads</dd>
            <dt>Output</dt>
            <dd>a verdict per claim, its conditions, a signed Proof Bundle</dd>
            <dt>Limits per run</dt>
            <dd>a CPU sandbox with no network, 30 compute minutes, $1 of inference</dd>
            <dt>Typical run</dt>
            <dd>1.6 minutes and $0.02 per claim (median, Set A)</dd>
          </dl>
        </div>
      </section>

      <section className="about-section" aria-labelledby="rule-h">
        <h2 id="rule-h">How the rule decides</h2>
        <p className="about-text">
          Every verdict is a pure function of the claim, our measurements and a fixed configuration whose hash is stored
          with it. The tolerance is the largest of 2% of the paper&rsquo;s value, twice the paper&rsquo;s own reported
          spread, and a fixed margin for the metric (0.005 for accuracy, F1 and AUC). Our 95% interval comes from the
          values of at least three seeds.
        </p>
        <ul className="rules">
          {RULES.map((r) => (
            <li key={r.kind}>
              <VerdictPill kind={r.kind} />
              <p>{r.text}</p>
            </li>
          ))}
        </ul>
        <p className="hand">a smaller run can confirm a gain, never accuse</p>
      </section>

      <section className="about-section" aria-labelledby="inside-h">
        <h2 id="inside-h">Inside a run</h2>
        <ol className="inside-steps">
          {STEPS.map((s, i) => (
            <li key={s.title}>
              <span className="inside-no" aria-hidden="true">
                {i + 1}
              </span>
              <span>
                <strong>{s.title}.</strong> {s.body}
              </span>
            </li>
          ))}
        </ol>
        <ArchitectureDiagram />
      </section>

      <section className="about-section" aria-labelledby="models-h">
        <h2 id="models-h">The models, and what they may not do</h2>
        <p className="about-text">
          Every model call goes to Nebius Token Factory, through the run&rsquo;s budget check and cost ledger. Each role
          maps to a Nemotron model by configuration, checked against the live model list at startup. Model output never
          decides a verdict and is never run as a privileged command: package installs, jobs and network access are done
          by deterministic code after validation.
        </p>
        <div className="table-wrap">
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
        </div>
        <p className="muted small">Each run page names the exact model behind every role in its cost receipt.</p>
      </section>

      <section className="about-section" aria-labelledby="results-h">
        <h2 id="results-h">How well it works</h2>
        <p className="kicker">ReproBench-mini Set A · 30 Oct 2026</p>
        <dl className="results">
          <div>
            <dt>claims judged right</dt>
            <dd>16/16</dd>
          </div>
          <div>
            <dt>false accusations</dt>
            <dd>0</dd>
          </div>
          <div>
            <dt>claim extraction F1</dt>
            <dd>1.00</dd>
          </div>
          <div>
            <dt>median run</dt>
            <dd>1.6 min</dd>
          </div>
          <div>
            <dt>10 papers, all in</dt>
            <dd>$0.28</dd>
          </div>
        </dl>
        <p className="about-text">
          Set A is 10 small papers we wrote ourselves, with known true claims and planted false ones: all 16 claims were
          judged right, with 0 false accusations. A false accusation, a true claim reported as not reproduced, is the
          error we guard against most. <span className="hand">real papers are next</span>
        </p>
        <h3>Verdicts across all runs</h3>
        {failed && <p className="muted">The statistics are not available right now.</p>}
        {stats && (
          <div className="table-wrap stats-table">
            <table>
              <caption>Verdicts across all runs</caption>
              <thead>
                <tr>
                  <th scope="col">Verdict</th>
                  <th scope="col" className="num">
                    Claims
                  </th>
                </tr>
              </thead>
              <tbody>
                {CAUTION_ORDER.map((k) => (
                  <tr key={k}>
                    <th scope="row">{VERDICT_HEADLINE[k]}</th>
                    <td className="num">{stats.runs_by_verdict[k] ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="about-section" aria-labelledby="faq-h">
        <h2 id="faq-h">Questions</h2>
        <div className="faq">
          <details>
            <summary>What does &ldquo;not reproduced under our conditions&rdquo; mean?</summary>
            <p>
              That our re-run, on the paper&rsquo;s setup and at full scale, measured something clearly short of the
              paper&rsquo;s number: the whole 95% interval, beyond the tolerance. It is a statement about our run, listed
              with its conditions, not a judgment of the authors.
            </p>
          </details>
          <details>
            <summary>Why only CPU, and only 30 minutes?</summary>
            <p>
              Experiments run in Nebius Token Factory Sandboxes, on CPU. Deep networks train far less than in the paper,
              so most real papers run at reduced scale, which can confirm a claimed gain but never accuse a paper.
            </p>
          </details>
          <details>
            <summary>Is my run public?</summary>
            <p>No. Runs are private to your browser session. The gallery is picked by hand.</p>
          </details>
          <details>
            <summary>What can go wrong?</summary>
            <p>
              A model reads the claims, and without an official repository the Coder re-implements the experiment from
              the paper. Each claim keeps its source and the setup check is shown, so you can see what was tested. The
              check is a tripwire, not a proof: numbers are looked up in the code, choices are taken as declared.
            </p>
          </details>
        </div>
      </section>

      <p className="muted small">
        Typefaces: Newsreader, Instrument Sans, Caveat and IBM Plex Mono, served from this site under the{' '}
        <a href="/fonts/LICENSES.txt">SIL Open Font License 1.1</a>.
      </p>
    </div>
  )
}

function ArchitectureDiagram() {
  const box = (x: number, y: number, w: number, title: string, sub: string) => (
    <g>
      <rect className="arch-box" x={x} y={y} width={w} height={52} rx={10} />
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
        aria-label="Architecture: the browser talks to the API and orchestrator on our host, which calls Nemotron models on Token Factory and launches three isolated stages with no secrets and a per-run bucket: provision in a locked-down container, smoke test in a Token Factory sandbox, and execute in a sandbox with no network. The verdict and the signed Proof Bundle come out of a pure function."
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
        <rect className="arch-zone" x={10} y={100} width={620} height={80} rx={12} />
        <text className="arch-zone-text" x={20} y={116}>
          isolated · no secrets · one bucket per run
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
      <figcaption>Dashed: where generated code runs, isolated, with no secrets and one bucket per run.</figcaption>
    </figure>
  )
}
