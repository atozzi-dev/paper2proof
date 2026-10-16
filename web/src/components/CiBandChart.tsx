import { chartSpec } from '../run/chart'
import type { Claim, Verdict } from '../types/api'

const W = 360
const H = 76
const PAD = 16
const AXIS_Y = 58

export function CiBandChart({ claim, verdict }: { claim: Claim | null; verdict: Verdict }) {
  const spec = chartSpec(claim, verdict)
  if (!spec || verdict.ci_low === null || verdict.ci_high === null || verdict.mean === null) return null
  const lo = verdict.ci_low
  const hi = verdict.ci_high
  const values = [lo, hi, verdict.mean, ...spec.markers.map((m) => m.at), ...(spec.band ?? [])]
  let min = Math.min(...values)
  let max = Math.max(...values)
  const span = max - min || Math.abs(max) || 1
  min -= span * 0.1
  max += span * 0.1
  const x = (value: number) => PAD + ((value - min) / (max - min)) * (W - 2 * PAD)
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={spec.label} preserveAspectRatio="xMidYMid meet">
        {spec.band && (
          <rect
            className="chart-band"
            x={x(spec.band[0])}
            y={10}
            width={Math.max(x(spec.band[1]) - x(spec.band[0]), 1)}
            height={AXIS_Y - 10}
          />
        )}
        <line className="chart-axis" x1={PAD} x2={W - PAD} y1={AXIS_Y} y2={AXIS_Y} />
        {spec.markers.map((m) => (
          <g key={m.label}>
            <line className="chart-marker" x1={x(m.at)} x2={x(m.at)} y1={8} y2={AXIS_Y} />
            <text className="chart-text" x={x(m.at)} y={AXIS_Y + 13} textAnchor="middle">
              {m.label}
            </text>
          </g>
        ))}
        <line className="chart-ci" x1={x(lo)} x2={x(hi)} y1={32} y2={32} />
        <line className="chart-ci" x1={x(lo)} x2={x(lo)} y1={26} y2={38} />
        <line className="chart-ci" x1={x(hi)} x2={x(hi)} y1={26} y2={38} />
        <circle className="chart-mean" cx={x(verdict.mean)} cy={32} r={4} />
      </svg>
      <figcaption aria-hidden="true">{spec.label}</figcaption>
    </figure>
  )
}
