// @vitest-environment node
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import openapiTS, { astToString } from 'openapi-typescript'
import { describe, expect, it } from 'vitest'

const web = (p: string) => fileURLToPath(new URL(`../${p}`, import.meta.url))

describe('generated API types', () => {
  it('match openapi.json (run: npm run gen:types)', async () => {
    const schema = JSON.parse(readFileSync(web('openapi.json'), 'utf8'))
    const fresh = astToString(await openapiTS(schema))
    const committed = readFileSync(web('src/types/openapi.ts'), 'utf8').replace(/\r\n/g, '\n')
    expect(committed.endsWith(fresh)).toBe(true)
  })
})
