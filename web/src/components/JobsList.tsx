import type { EventPayloads } from '../types/api'
import { JOB_KIND_TEXT } from '../run/labels'

type Jobs = Readonly<Record<string, EventPayloads['job.status']>>

/** Every cloud job of the run, in the order it was first seen, with its latest state. */
export function JobsList({ jobs }: { jobs: Jobs }) {
  const all = Object.values(jobs)
  if (all.length === 0) return <p className="muted">No cloud job yet.</p>
  return (
    <ul className="jobs" aria-label="Cloud jobs">
      {all.map((job) => (
        <li key={job.name}>
          <strong>{JOB_KIND_TEXT[job.kind]}</strong> <code>{job.name}</code>{' '}
          <span className="muted">{job.state}</span>
        </li>
      ))}
    </ul>
  )
}
