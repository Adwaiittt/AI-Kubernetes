# Dashboard

Next.js (App Router) + TypeScript + Tailwind. No component library.

- **Incidents** (`/`): status badges, auto-refresh while anything is in flight
- **Incident** (`/incidents/[id]`): live agent timeline over SSE, root-cause report, proposed fix with
  server-side dry-run diff, and Approve / Reject behind a confirm dialog
- **New investigation** (`/incidents/new`)
- **Audit log** (`/audit`, `?incident=<id>` to filter)

```bash
npm install
npm run dev          # http://localhost:3000, expects the API at NEXT_PUBLIC_API_URL (default :8000)
npm run lint && npm run typecheck && npm test && npm run build
```

## Typed API client

`src/lib/api/schema.ts` is generated from the FastAPI OpenAPI schema. After changing the API:

```bash
cd ../backend && uv run python -m app.export_openapi ../frontend/openapi.json
cd ../frontend && npm run gen:api
```

A backend test fails if `openapi.json` is stale.

## Approver key

Dry-run / approve / reject need an approver API key (`APPROVER_API_KEYS` on the backend). Enter it
with **Set approver key**; it is kept in `sessionStorage` (this tab only) and sent only as the
`X-API-Key` header to the API.
