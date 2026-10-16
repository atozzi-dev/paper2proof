import { describe, expect, it, vi } from 'vitest'
import { SseParser } from '../src/api/sseParser'
import { streamRunEvents } from '../src/api/sse'
import type { RunEvent } from '../src/types/api'

const enc = new TextEncoder()

function frame(id: number, type: string, payload: object): string {
  return `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(payload)}\n\n`
}

/** A response whose body sends `chunks` and then ends cleanly, or fails when `fail` is set. */
function sseResponse(chunks: string[], fail = false): Response {
  const queue = [...chunks]
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      const next = queue.shift()
      if (next !== undefined) controller.enqueue(enc.encode(next))
      else if (fail) controller.error(new TypeError('network error'))
      else controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

const noSleep = () => Promise.resolve()

describe('SseParser', () => {
  it('parses frames split across chunks, skips heartbeats and joins multi-line data', () => {
    const parser = new SseParser()
    const out = [
      ...parser.push(': hb\n\nid: 1\nevent: run.st'),
      ...parser.push('atus\ndata: {"status":\ndata: "RUNNING"}\r\n\r\n'),
    ]
    expect(out).toEqual([{ id: '1', event: 'run.status', data: '{"status":\n"RUNNING"}' }])
  })
})

describe('streamRunEvents', () => {
  it('resumes with Last-Event-ID after the stream drops', async () => {
    const calls: RequestInit[] = []
    const urls: string[] = []
    const responses = [
      sseResponse(
        [
          frame(1, 'run.status', { status: 'RUNNING' }),
          frame(2, 'step.started', { step: 'INGESTING', attempt: 1 }),
          frame(3, 'step.finished', { step: 'INGESTING', attempt: 1, ok: true, duration_ms: 5 }),
        ],
        true,
      ),
      sseResponse([frame(4, 'run.status', { status: 'COMPLETED' })]),
    ]
    const fetchImpl = vi.fn((url: string, init: RequestInit) => {
      urls.push(url)
      calls.push(init)
      return Promise.resolve(responses.shift()!)
    })
    const events: RunEvent[] = []
    let done = false
    await streamRunEvents({
      runId: 'r_x',
      onEvent: (e) => {
        events.push(e)
        if (e.type === 'run.status' && e.payload.status === 'COMPLETED') done = true
      },
      isDone: () => done,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      sleep: noSleep,
    }).finished

    expect(events.map((e) => e.id)).toEqual([1, 2, 3, 4])
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(new Headers(calls[0].headers).get('Last-Event-ID')).toBeNull()
    expect(new Headers(calls[1].headers).get('Last-Event-ID')).toBe('3')
    expect(urls[1]).toBe('/api/runs/r_x/events')
  })

  it('never delivers the same event twice', async () => {
    const responses = [
      sseResponse([frame(1, 'run.status', { status: 'RUNNING' })], true),
      sseResponse([frame(1, 'run.status', { status: 'RUNNING' }), frame(2, 'run.status', { status: 'FAILED' })]),
    ]
    const ids: number[] = []
    let done = false
    await streamRunEvents({
      runId: 'r_x',
      onEvent: (e) => {
        ids.push(e.id)
        done = e.type === 'run.status' && e.payload.status === 'FAILED'
      },
      isDone: () => done,
      fetchImpl: (() => Promise.resolve(responses.shift()!)) as unknown as typeof fetch,
      sleep: noSleep,
    }).finished
    expect(ids).toEqual([1, 2])
  })

  it('asks for a timed replay first, then resumes live with Last-Event-ID', async () => {
    const urls: string[] = []
    const headers: Headers[] = []
    const responses = [
      sseResponse([frame(1, 'run.status', { status: 'RUNNING' })], true),
      sseResponse([frame(2, 'run.status', { status: 'COMPLETED' })]),
    ]
    let done = false
    await streamRunEvents({
      runId: 'r_x',
      replay: true,
      speed: 20,
      onEvent: (e) => {
        done = e.type === 'run.status' && e.payload.status === 'COMPLETED'
      },
      isDone: () => done,
      fetchImpl: ((url: string, init: RequestInit) => {
        urls.push(url)
        headers.push(new Headers(init.headers))
        return Promise.resolve(responses.shift()!)
      }) as unknown as typeof fetch,
      sleep: noSleep,
    }).finished
    expect(urls).toEqual(['/api/runs/r_x/events?replay=1&speed=20', '/api/runs/r_x/events'])
    expect(headers[1].get('Last-Event-ID')).toBe('1')
  })

  it('connects once for a finished run and resumes only after a cut', async () => {
    const responses = [
      sseResponse([frame(1, 'step.started', { step: 'INGESTING', attempt: 1 })], true),
      sseResponse([frame(2, 'step.finished', { step: 'INGESTING', attempt: 1, ok: true, duration_ms: 1 })]),
    ]
    const fetchImpl = vi.fn(() => Promise.resolve(responses.shift()!))
    const ids: number[] = []
    await streamRunEvents({
      runId: 'r_x',
      onEvent: (e) => ids.push(e.id),
      isDone: (cleanEnd) => cleanEnd, // the run page knows the run is over from GET /api/runs/{id}
      fetchImpl: fetchImpl as unknown as typeof fetch,
      sleep: noSleep,
    }).finished
    expect(ids).toEqual([1, 2])
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })

  it('falls back to polling events.json when streams are rate limited', async () => {
    const urls: string[] = []
    const fetchImpl = (url: string) => {
      urls.push(url)
      if (url.endsWith('/events')) return Promise.resolve(new Response('{}', { status: 429 }))
      const page = {
        events: [
          { id: 1, run_id: 'r_x', ts: '2026-10-16T00:00:00Z', type: 'run.status', payload: { status: 'COMPLETED' } },
        ],
      }
      return Promise.resolve(new Response(JSON.stringify(page), { status: 200 }))
    }
    const modes: string[] = []
    let done = false
    await streamRunEvents({
      runId: 'r_x',
      onEvent: (e) => {
        done = e.type === 'run.status' && e.payload.status === 'COMPLETED'
      },
      onMode: (m) => modes.push(m),
      isDone: () => done,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      sleep: noSleep,
    }).finished
    expect(urls).toEqual(['/api/runs/r_x/events', '/api/runs/r_x/events.json?after=0'])
    expect(modes).toContain('polling')
    expect(done).toBe(true)
  })

  it('stops for good on 404 and reports it', async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(new Response('{"error":"not found"}', { status: 404 })))
    const modes: string[] = []
    await streamRunEvents({
      runId: 'r_x',
      onEvent: () => {},
      onMode: (m) => modes.push(m),
      isDone: () => false,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      sleep: noSleep,
    }).finished
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(modes.at(-1)).toBe('not_found')
  })
})
