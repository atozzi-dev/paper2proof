import { chartSpec, ticks, type Zone } from '../run/chart'
import { fmtNum } from '../run/verdictCopy'
import type { Claim, Verdict } from '../types/api'

const W = 600
const H = 108
const PAD = 22
const AXIS_Y = 72
const CI_Y = 48

/** Where the zone's label fits inside it, away from the paper's line; null when nowhere. */
function zoneLabel(zone: Zone, from: number, to: number, paper: number | null) {
  const width = zone.text.length * 6 + 8 // 11px text
  const options = [
    { x: to - 8, anchor: 'end' as const, left: to - 8 - width, right: to - 4 },
    { x: from + 8, anchor: 'start' as const, left: from + 4, right: from + 8 + width },
  ]
  return (
    options.find((o) => o.left >= from && o.right <= to && (paper === null || paper < o.left - 6 || paper > o.right + 6)) ??
    null
  )
}

/** Our 95% interval (blue) against the zone the rule compares it with (green), with the
 * paper's own value marked in yellow. Drawn from the verdict's numbers only. */
export function CiBandChart({ claim, verdict }: { claim: Claim | null; verdict: Verdict }) {
  const spec = chartSpec(claim, verdict)
  if (!spec || verdict.ci_low === null || verdict.ci_high === null || verdict.mean === null) return null
  const lo = verdict.ci_low
  const hi = verdict.ci_high
  const mean = verdict.mean
  const zone = spec.zone
  const values = [
    lo,
    hi,
    mean,
    ...(spec.paper ? [spec.paper.at] : []),
    ...(spec.zero ? [0] : []),
    ...(zone ? [zone.from, zone.to].filter((z): z is number => z !== null) : []),
  ]
  let min = Math.min(...values)
  let max = Math.max(...values)
  const span = max - min || Math.abs(max) || 1
  min -= span * 0.12
  max += span * 0.12
  const x = (value: number) => PAD + ((value - min) / (max - min)) * (W - 2 * PAD)
  const zoneFrom = zone ? x(zone.from ?? min) : 0
  const zoneTo = zone ? x(zone.to ?? max) : 0
  const paperX = spec.paper ? x(spec.paper.at) : 0
  const paperW = spec.paper ? spec.paper.text.length * 6.4 + 12 : 0
  const paperLeft = Math.min(Math.max(paperX - paperW / 2, 0), W - paperW)
  const sign = spec.zero ? (t: number) => (t > 0 ? `+${fmtNum(t)}` : fmtNum(t)) : fmtNum
  // The zone's label goes inside the drawing only where it fits without crossing the paper's
  // line; the caption always states it.
  const zoneText = zone ? zoneLabel(zone, zoneFrom, zoneTo, spec.paper ? paperX : null) : null
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={spec.label} preserveAspectRatio="xMidYMid meet">
        {zone && (
          <g>
            <rect className="chart-zone" x={zoneFrom} y={22} width={Math.max(zoneTo - zoneFrom, 1)} height={AXIS_Y - 22} rx={6} />
            {zoneText && (
              <text className="chart-zone-text" x={zoneText.x} y={AXIS_Y - 8} textAnchor={zoneText.anchor}>
                {zone.text}
              </text>
            )}
          </g>
        )}
        <line className="chart-axis" x1={PAD} x2={W - PAD} y1={AXIS_Y} y2={AXIS_Y} />
        {ticks(min, max).map((t) => (
          <g key={t}>
            <line className="chart-axis" x1={x(t)} x2={x(t)} y1={AXIS_Y} y2={AXIS_Y + 5} />
            <text className="chart-tick" x={x(t)} y={AXIS_Y + 22} textAnchor="middle">
              {sign(t)}
            </text>
          </g>
        ))}
        {spec.zero && <line className="chart-zero" x1={x(0)} x2={x(0)} y1={20} y2={AXIS_Y} />}
        {spec.paper && (
          <g>
            <rect className="chart-paper-chip" x={paperLeft} y={2} width={paperW} height={17} rx={3} />
            <text className="chart-paper-text" x={paperLeft + paperW / 2} y={14.5} textAnchor="middle">
              {spec.paper.text}
            </text>
            <line className="chart-paper" x1={paperX} x2={paperX} y1={19} y2={AXIS_Y} />
          </g>
        )}
        <line className="chart-ci" x1={x(lo)} x2={x(hi)} y1={CI_Y} y2={CI_Y} />
        <circle className="chart-mean" cx={x(mean)} cy={CI_Y} r={6} />
        {/* beside the interval, on the side with room, so it never sits on the paper's line */}
        <text
          className="chart-us"
          x={x(hi) < W - 110 ? x(hi) + 12 : x(lo) - 12}
          y={CI_Y + 6}
          textAnchor={x(hi) < W - 110 ? 'start' : 'end'}
        >
          us, {verdict.n_seeds} seed{verdict.n_seeds === 1 ? '' : 's'}
        </text>
      </svg>
      <figcaption>
        <strong>Our 95% interval</strong> in blue, the mean as a dot; the paper&rsquo;s value in yellow
        {zone && <>; green: {zone.text}</>}.
      </figcaption>
    </figure>
  )
}
