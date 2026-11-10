-- AI Portfolio Control Dashboard: Phase 1 schema + RLS
-- Target: Supabase Postgres (roles anon / authenticated / service_role and auth.jwt() exist there).
-- The Control API connects with the service role (or postgres) and bypasses RLS.

begin;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table public.projects (
  id          text primary key check (id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),  -- e.g. 'recruit-ai'
  name        text not null,
  description text,
  live_url    text,
  repo_url    text,
  tags        text[] not null default '{}',
  sort_order  int not null default 0,
  is_public   boolean not null default true
);

-- Live, editable configuration per project. Defaults are the "fail cheap" state.
create table public.project_config (
  project_id           text primary key references public.projects(id) on delete cascade,
  enabled              boolean not null default false,
  llm_mode             text not null default 'free' check (llm_mode in ('free','paid','off')),
  model_free           text not null default 'groq/llama-3.1-8b-instant',
  model_paid           text not null default 'openai/gpt-4o-mini',
  voice_calls_enabled  boolean not null default false,
  voice_model          text,
  daily_budget_usd     numeric(10,2) not null default 1.00 check (daily_budget_usd >= 0),
  max_runs_per_visitor int not null default 3 check (max_runs_per_visitor >= 0),
  extra                jsonb not null default '{}',   -- project-specific, e.g. {"score_threshold": 70}
  updated_at           timestamptz not null default now(),
  -- Set by the budget cron when it downgrades a project, cleared by the owner.
  budget_tripped_at    timestamptz
);

-- Render services belonging to each project
create table public.project_services (
  id                text primary key,       -- Render service id, e.g. 'srv-xxxx'
  project_id        text not null references public.projects(id) on delete cascade,
  label             text,                   -- 'backend', 'voice-worker'
  last_known_status text not null default 'unknown'
                    check (last_known_status in ('live','suspended','starting','unknown')),
  updated_at        timestamptz not null default now()
);
create index on public.project_services(project_id);

-- Per-project API tokens. Only a SHA-256 hash is stored; the plaintext lives in the
-- project's Render env as PROJECT_TOKEN.
create table public.project_tokens (
  token_sha256 text primary key,
  project_id   text not null references public.projects(id) on delete cascade,
  created_at   timestamptz not null default now(),
  revoked_at   timestamptz
);

-- Usage events emitted by projects (no raw PII: visitor_hash is an HMAC computed by the Control API)
create table public.events (
  id           bigserial primary key,
  project_id   text not null references public.projects(id) on delete cascade,
  type         text not null check (type ~ '^[a-z][a-z0-9_]{0,63}$'),
  model        text,
  tokens_in    int,
  tokens_out   int,
  cost_usd     numeric(12,6),
  latency_ms   int,
  score        numeric,
  visitor_hash text,
  meta         jsonb not null default '{}',
  created_at   timestamptz not null default now()
);
create index events_project_created on public.events(project_id, created_at desc);
create index events_visitor_today   on public.events(project_id, visitor_hash, type, created_at)
  where visitor_hash is not null;

-- Dashboard owner allow-list (emails that may use /admin)
create table public.owners (
  email text primary key
);

-- Keep updated_at fresh
create function public.touch_updated_at() returns trigger language plpgsql as $$
begin new.updated_at := now(); return new; end $$;
create trigger project_config_touch before update on public.project_config
  for each row execute function public.touch_updated_at();
create trigger project_services_touch before update on public.project_services
  for each row execute function public.touch_updated_at();

-- Every new project gets a config row with safe defaults
create function public.create_default_config() returns trigger language plpgsql as $$
begin insert into public.project_config(project_id) values (new.id) on conflict do nothing; return new; end $$;
create trigger projects_default_config after insert on public.projects
  for each row execute function public.create_default_config();

-- ---------------------------------------------------------------------------
-- Public views (portfolio). These run with the view owner's rights so anon can
-- read aggregates without being able to read raw events, config or costs caps.
-- ---------------------------------------------------------------------------

-- A "run" is one visitor-initiated attempt that passed /check (type = 'run_started').
create view public.project_stats as
select p.id                                                                  as project_id,
       count(e.*) filter (where e.type = 'run_started')                      as total_runs,
       count(e.*) filter (where e.type = 'run_started'
                            and e.created_at > now() - interval '7 days')    as runs_7d,
       round(avg(e.latency_ms) filter (where e.type <> 'llm_call'))          as avg_latency_ms,
       round(avg(e.score)::numeric, 1)                                       as avg_score,
       round((coalesce(sum(e.cost_usd), 0)
              / nullif(count(e.*) filter (where e.type = 'run_started'), 0))::numeric, 4)
                                                                             as cost_per_run_usd,
       max(e.created_at) filter (where e.type = 'run_started')               as last_run_at
from public.projects p
left join public.events e on e.project_id = p.id
where p.is_public
group by p.id;

-- live | waking | paused, without exposing budgets or models
create view public.project_public_status as
select p.id as project_id,
       case
         when not c.enabled or c.llm_mode = 'off' then 'paused'
         when exists (select 1 from public.project_services s
                      where s.project_id = p.id
                        and s.last_known_status in ('starting','suspended')) then 'waking'
         else 'live'
       end as status
from public.projects p
join public.project_config c on c.project_id = p.id
where p.is_public;

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------

create function public.is_owner() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.owners o
                 where lower(o.email) = lower(coalesce(auth.jwt() ->> 'email', '')))
$$;
revoke all on function public.is_owner() from public;
grant execute on function public.is_owner() to anon, authenticated;  -- anon always gets false

alter table public.projects         enable row level security;
alter table public.project_config   enable row level security;
alter table public.project_services enable row level security;
alter table public.project_tokens   enable row level security;
alter table public.events           enable row level security;
alter table public.owners           enable row level security;

-- Start from zero, then grant exactly what each role needs.
revoke all on public.projects, public.project_config, public.project_services,
              public.project_tokens, public.events, public.owners,
              public.project_stats, public.project_public_status
  from anon, authenticated;

-- anon: public project list + aggregated stats + status
grant select on public.projects, public.project_stats, public.project_public_status to anon, authenticated;
create policy projects_public_read on public.projects
  for select to anon, authenticated using (is_public or public.is_owner());

-- owner (authenticated + allow-listed): read everything except tokens; edit project metadata.
-- Config edits and power toggles go through the Control API so Render stays in sync.
grant select on public.project_config, public.project_services, public.events to authenticated;
grant insert, update, delete on public.projects to authenticated;
create policy config_owner_read   on public.project_config   for select to authenticated using (public.is_owner());
create policy services_owner_read on public.project_services for select to authenticated using (public.is_owner());
create policy events_owner_read   on public.events           for select to authenticated using (public.is_owner());
create policy projects_owner_write on public.projects for all to authenticated
  using (public.is_owner()) with check (public.is_owner());

-- project_tokens and owners: no policies → only service role / postgres can touch them.

commit;
