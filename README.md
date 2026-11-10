# Dashboard

Public AI portfolio plus a private owner dashboard that controls each project (Render power, free/paid LLM mode, budgets).


Foundation for the AI Portfolio Control Dashboard: database, Control API, and the `control_client`
library each project uses. Built from the spec's defaults; values marked `REPLACE-ME` wait on the
Section 12 answers.

```
supabase/migrations/0001_init.sql   schema, views, RLS
supabase/seed.sql                   owner email + Recruit AI placeholder row
control_api/                        FastAPI service for Render (render.yaml at repo root)
control_client/                     pip-installable client for project backends
scripts/new_project_token.py        mint a PROJECT_TOKEN (only its hash goes in the DB)
tests/                              19 tests against real Postgres (RLS, API, client)
```

## Setup

1. **Supabase:** create a project, run `0001_init.sql` then `seed.sql` in the SQL editor
   (put your real email in `owners`).
2. **Token:** `python scripts/new_project_token.py recruit-ai`, run the printed SQL in Supabase,
   and put `PROJECT_TOKEN=...` in Recruit AI's Render env.
3. **Control API:** deploy with `render.yaml`, then set `DATABASE_URL` (Supabase connection
   string), `RENDER_API_KEY`, `SUPABASE_URL` (or `SUPABASE_JWT_SECRET` for legacy HS256),
   `OWNER_EMAILS`, `CORS_ORIGINS`. `VISITOR_HASH_KEY` and `CRON_SECRET` are generated.
4. **Recruit AI:** `pip install "control-client[llm] @ git+https://github.com/<you>/<repo>#subdirectory=control_client"`
   and set `CONTROL_API_URL`.

All secrets live only in Render env vars. The public endpoints never return budgets, models,
spend or raw events.

## Endpoints

| Who | Endpoint | Purpose |
|---|---|---|
| Public | `GET /public/projects`, `GET /public/projects/{id}/status` | list + status (live/waking/paused) + stats |
| Project (`PROJECT_TOKEN`) | `GET /projects/{id}/config` | current config |
| | `POST /projects/{id}/check` `{visitor_id, action: run\|voice_call}` | gate before an expensive action |
| | `POST /projects/{id}/events` | log usage |
| Owner (Supabase JWT + allow-list) | `PATCH /admin/projects/{id}/config` | modes, models, budgets, `extra` (merged) |
| | `POST /admin/projects/{id}/power` `{on}` | suspend/resume all Render services |
| | `GET /admin/projects/{id}/services` | refresh status from Render + health probe |
| | `GET /admin/usage` | spend today/month, runs today |
| | `POST /admin/kill-switch` `{mode: free\|off}` | everything to Free/Off, voice off |
| Cron (`CRON_SECRET`) | `POST /internal/enforce-budgets` | over-budget projects → Free (or Off), voice off |
| Anyone | `GET /healthz` | also keeps Supabase awake when pinged daily |

## Choices that differ from the spec

- **Runs are counted from `/check`.** A granted check writes a `run_started` event, which drives
  both the per-visitor limit and the public "runs" stats. Projects can't write that type
  themselves, so stats can't be inflated and a run isn't counted twice.
- **Project tokens are stored hashed** in `project_tokens`, so you can add or revoke projects
  without redeploying the Control API.
- **Voice calls have their own per-visitor limit** (`extra.max_voice_calls_per_visitor`, default 1).
- **"Today" is UTC** for budgets and limits.
- **Stale config never runs paid.** If the Control API is unreachable, the client keeps the last
  config but drops to the free model and turns voice off.
- **Over-budget mode:** set `extra.over_budget_mode = "off"` to pause instead of dropping to Free.
- **Realtime:** anon can't read `events` under RLS, so Supabase Realtime on `events` won't reach the
  portfolio. Phase 4 will poll `/public/projects` (or broadcast a small "stats changed" signal).

## Running tests

Needs a local Postgres. Point `TEST_PG_ADMIN_DSN` at it (default `postgresql://postgres@localhost:54329/postgres`).

```
pip install fastapi httpx "psycopg[binary,pool]" pydantic-settings pyjwt pytest
pytest
```
