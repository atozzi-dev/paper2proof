# Paper2Proof web

React + Vite + TypeScript single-page app, served by the API in production. Hash routes:
`#/` (start a run), `#/runs/<id>` (live run, `?replay=1` for a timed replay), `#/gallery`, `#/about`.

```bash
npm ci
npm run dev        # proxies /api to http://127.0.0.1:8000
npm test           # vitest
npm run lint       # oxlint
npm run build
npm run gen:types  # openapi.json from the FastAPI app, then src/types/openapi.ts
npm run test:e2e   # Playwright smoke test on the production build, backend mocked
```

- API types are generated, never written by hand: change the pydantic models, then run
  `npm run gen:types` (needs `uv`). Tests fail when `openapi.json` or `src/types/openapi.ts` is stale.
- Run events come over SSE (`fetch` + `Last-Event-ID` on reconnect), with a polling fallback on
  `events.json`.
- Text from papers, generated code and logs is rendered as plain text. The Judge's explanation is
  markdown rendered without raw HTML, links or images.
- `npm run test:e2e` uses the installed Chrome; set `PLAYWRIGHT_CHANNEL=chromium` after
  `npx playwright install chromium` to use Playwright's own build.
