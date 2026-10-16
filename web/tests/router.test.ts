import { describe, expect, it } from 'vitest'
import { parseRoute } from '../src/router'

describe('parseRoute', () => {
  it('parses run routes with and without replay', () => {
    expect(parseRoute('#/runs/r_x')).toEqual({ page: 'run', id: 'r_x', replay: false })
    expect(parseRoute('#/runs/r_x?replay=1')).toEqual({ page: 'run', id: 'r_x', replay: true })
    expect(parseRoute('')).toEqual({ page: 'home' })
  })

  it('treats a malformed escape as not found instead of crashing', () => {
    expect(parseRoute('#/runs/%E0%A4%A')).toEqual({ page: 'not_found' })
  })
})
