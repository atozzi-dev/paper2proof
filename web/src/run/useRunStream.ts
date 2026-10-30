import { useEffect, useReducer, useRef, useState } from 'react'
import { streamRunEvents, type StreamMode } from '../api/sse'
import { TERMINAL_STATUSES, type RunEvent } from '../types/api'
import { initialRunState, reduceRun, type RunState } from './state'

/** Live run state from the event stream. `knownTerminal` comes from GET /api/runs/{id}. */
export function useRunStream(runId: string, replay: boolean, knownTerminal: boolean, speed = 10) {
  const [state, dispatch] = useReducer(
    (s: RunState, e: RunEvent | null) => (e ? reduceRun(s, e) : initialRunState()),
    undefined,
    initialRunState,
  )
  const [mode, setMode] = useState<StreamMode>('connecting')
  const terminalEvent = useRef(false)
  const terminalRun = useRef(knownTerminal)
  useEffect(() => {
    terminalRun.current = knownTerminal
  }, [knownTerminal])

  useEffect(() => {
    dispatch(null)
    terminalEvent.current = false
    const handle = streamRunEvents({
      runId,
      replay,
      speed,
      onEvent: (e) => {
        if (e.type === 'run.status' && TERMINAL_STATUSES.has(e.payload.status)) terminalEvent.current = true
        dispatch(e)
      },
      onMode: setMode,
      isDone: (cleanEnd) => terminalEvent.current || (cleanEnd && terminalRun.current),
    })
    return () => handle.close()
  }, [runId, replay, speed])

  return { state, mode }
}
