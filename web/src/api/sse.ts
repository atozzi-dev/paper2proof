// Run event stream: SSE over fetch (so the client can send Last-Event-ID itself when it
// reconnects), with backoff and a polling fallback on events.json.
import { toRunEvent, type EventEnvelope, type RunEvent } from '../types/api'
import { SseParser } from './sseParser'

export type StreamMode = 'connecting' | 'live' | 'reconnecting' | 'polling' | 'closed' | 'not_found'

export interface StreamOptions {
  runId: string
  /** Timed replay of a finished run (gallery). Only the first connection asks for it. */
  replay?: boolean
  speed?: number
  onEvent: (event: RunEvent) => void
  onMode?: (mode: StreamMode) => void
  /** True once every event of the run has been delivered: the stream then stops for good.
   * `cleanEnd` tells whether the last connection ended normally (not cut or failed). */
  isDone: (cleanEnd: boolean) => boolean
  fetchImpl?: typeof fetch
  sleep?: (ms: number) => Promise<void>
  /** Failed connections in a row before switching to polling. */
  maxRetries?: number
  pollMs?: number
}

export interface StreamHandle {
  close: () => void
  finished: Promise<void>
}

class NotFound extends Error {}
class RateLimited extends Error {}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

export function backoffMs(attempt: number): number {
  return Math.min(500 * 2 ** Math.max(attempt - 1, 0), 10_000)
}

export function streamRunEvents(opts: StreamOptions): StreamHandle {
  const fetchImpl = opts.fetchImpl ?? ((input, init) => fetch(input, init))
  const sleep = opts.sleep ?? defaultSleep
  const maxRetries = opts.maxRetries ?? 5
  const pollMs = opts.pollMs ?? 3000
  const controller = new AbortController()
  const base = `/api/runs/${encodeURIComponent(opts.runId)}`
  let lastId = 0
  let closed = false

  const setMode = (mode: StreamMode) => opts.onMode?.(mode)

  /** Hands over events in order, once each; returns how many were new. */
  const deliver = (event: RunEvent | null): number => {
    if (!event || event.id <= lastId) return 0
    lastId = event.id
    opts.onEvent(event)
    return 1
  }

  async function connectOnce(): Promise<number> {
    const headers: Record<string, string> = { Accept: 'text/event-stream' }
    let url = `${base}/events`
    if (lastId > 0) headers['Last-Event-ID'] = String(lastId)
    else if (opts.replay) url += `?replay=1&speed=${opts.speed ?? 10}`
    const res = await fetchImpl(url, {
      headers,
      signal: controller.signal,
      credentials: 'same-origin',
      cache: 'no-store',
    })
    if (res.status === 404) throw new NotFound()
    if (res.status === 429) throw new RateLimited()
    if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`)
    setMode('live')
    const parser = new SseParser()
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
    let received = 0
    for (;;) {
      const { value, done } = await reader.read()
      if (done) return received
      for (const frame of parser.push(value)) {
        let payload: unknown
        try {
          payload = JSON.parse(frame.data)
        } catch {
          continue
        }
        received += deliver(toRunEvent(Number(frame.id), frame.event, payload))
      }
    }
  }

  /** Polls events.json until the run is over; returns true when it is. */
  async function poll(): Promise<boolean> {
    setMode('polling')
    for (;;) {
      let complete = false
      try {
        const res = await fetchImpl(`${base}/events.json?after=${lastId}`, {
          signal: controller.signal,
          credentials: 'same-origin',
          cache: 'no-store',
        })
        if (res.status === 404) throw new NotFound()
        if (res.ok) {
          const page = (await res.json()) as { events: EventEnvelope[] }
          for (const e of page.events) deliver(toRunEvent(e.id, e.type, e.payload))
          complete = true
        }
      } catch (err) {
        if (err instanceof NotFound) throw err
      }
      if (closed) return false
      if (opts.isDone(complete)) return true
      await sleep(pollMs)
    }
  }

  async function run(): Promise<void> {
    setMode('connecting')
    let failures = 0
    try {
      // Always connect at least once: a finished run still has its events to show.
      for (;;) {
        let clean = false
        try {
          const received = await connectOnce()
          clean = true
          // The server ends a stream only once the run is over; anything else is a cut.
          failures = received > 0 ? 0 : failures + 1
        } catch (err) {
          if (err instanceof NotFound) throw err
          if (closed) return
          if (err instanceof RateLimited) {
            if (await poll()) setMode('closed')
            return
          }
          failures += 1
        }
        if (closed) return
        if (opts.isDone(clean)) break
        if (failures > maxRetries) {
          if (await poll()) setMode('closed')
          return
        }
        setMode('reconnecting')
        await sleep(backoffMs(failures))
      }
    } catch (err) {
      if (err instanceof NotFound) {
        setMode('not_found')
        return
      }
      throw err
    }
    setMode('closed')
  }

  const finished = run().catch(() => setMode('closed'))

  return {
    close: () => {
      closed = true
      controller.abort()
    },
    finished,
  }
}
