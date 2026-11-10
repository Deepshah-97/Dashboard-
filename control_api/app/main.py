"""Control API: the only component that holds the Render key and writes project config."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from . import db
from .auth import hash_visitor, require_cron, require_owner, require_project
from .render import RenderClient, RenderError, probe_health
from .settings import Settings, get_settings

# Event types that only the API itself may write (they drive rate limits and run counts).
RESERVED_EVENT_TYPES = {"run_started", "voice_call_started"}
MODEL_RE = r"^[A-Za-z0-9_.:/\-]{1,120}$"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool(get_settings().database_url)
    yield
    await db.close_pool()


app = FastAPI(title="Portfolio Control API", lifespan=lifespan)
_settings_for_cors = None
try:
    _settings_for_cors = get_settings()
except Exception:  # settings are validated again at startup; CORS is optional
    pass
if _settings_for_cors and _settings_for_cors.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings_for_cors.cors_origins,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
    )


# ---------------------------------------------------------------------------
# Shared queries
# ---------------------------------------------------------------------------

async def _load_config(c, project_id: str, lock: bool = False) -> dict:
    sql = "select * from project_config where project_id = %s" + (" for update" if lock else "")
    row = await (await c.execute(sql, (project_id,))).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown project")
    return row


async def _spend_today(c, project_id: str) -> Decimal:
    row = await (await c.execute(
        """select coalesce(sum(cost_usd), 0) as spend from events
           where project_id = %s and created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'""",
        (project_id,),
    )).fetchone()
    return Decimal(row["spend"])


def _active_model(cfg: dict) -> str | None:
    return {"free": cfg["model_free"], "paid": cfg["model_paid"]}.get(cfg["llm_mode"])


def _project_view(cfg: dict) -> dict:
    return {
        "project_id": cfg["project_id"],
        "enabled": cfg["enabled"],
        "llm_mode": cfg["llm_mode"],
        "model": _active_model(cfg),
        "model_free": cfg["model_free"],
        "model_paid": cfg["model_paid"],
        "voice_calls_enabled": cfg["voice_calls_enabled"],
        "voice_model": cfg["voice_model"],
        "extra": cfg["extra"],
        "updated_at": cfg["updated_at"],
    }


# ---------------------------------------------------------------------------
# Health (also serves as the Supabase keep-alive: it touches the DB)
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    async with db.conn() as c:
        await c.execute("select 1")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Public (no auth). Never returns budgets, models, or spend.
# ---------------------------------------------------------------------------

_PUBLIC_SQL = """
select p.id, p.name, p.description, p.live_url, p.repo_url, p.tags,
       st.status, s.total_runs, s.runs_7d, s.avg_latency_ms, s.avg_score, s.cost_per_run_usd, s.last_run_at
from projects p
join project_public_status st on st.project_id = p.id
join project_stats s on s.project_id = p.id
where p.is_public
order by p.sort_order, p.name
"""


@app.get("/public/projects")
async def public_projects():
    async with db.conn() as c:
        return await (await c.execute(_PUBLIC_SQL)).fetchall()


@app.get("/public/projects/{project_id}/status")
async def public_status(project_id: str):
    async with db.conn() as c:
        row = await (await c.execute(
            "select project_id, status from project_public_status where project_id = %s", (project_id,)
        )).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown project")
    return row


# ---------------------------------------------------------------------------
# Project → API (PROJECT_TOKEN)
# ---------------------------------------------------------------------------

@app.get("/projects/{project_id}/config")
async def project_config(project_id: str = Depends(require_project)):
    async with db.conn() as c:
        return _project_view(await _load_config(c, project_id))


class CheckIn(BaseModel):
    visitor_id: str = Field(min_length=1, max_length=200)
    action: Literal["run", "voice_call"] = "run"


class CheckOut(BaseModel):
    allowed: bool
    reason: str
    run_id: int | None = None
    remaining_today: int | None = None


@app.post("/projects/{project_id}/check", response_model=CheckOut)
async def project_check(
    body: CheckIn,
    project_id: str = Depends(require_project),
    settings: Settings = Depends(get_settings),
):
    """Call before any expensive action. A granted 'run' is recorded as a run_started event."""
    vh = hash_visitor(body.visitor_id, settings)
    async with db.conn() as c, c.transaction():
        cfg = await _load_config(c, project_id)
        if not cfg["enabled"]:
            return CheckOut(allowed=False, reason="disabled")
        if cfg["llm_mode"] == "off":
            return CheckOut(allowed=False, reason="paused")
        if await _spend_today(c, project_id) >= cfg["daily_budget_usd"]:
            return CheckOut(allowed=False, reason="over_budget")

        if body.action == "voice_call":
            if not cfg["voice_calls_enabled"]:
                return CheckOut(allowed=False, reason="voice_disabled")
            limit, event_type = int(cfg["extra"].get("max_voice_calls_per_visitor", 1)), "voice_call_started"
        else:
            limit, event_type = cfg["max_runs_per_visitor"], "run_started"

        # Serialise concurrent checks for the same visitor so the limit can't be raced.
        await c.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"{project_id}:{vh}:{event_type}",))
        used = (await (await c.execute(
            """select count(*) as n from events
               where project_id = %s and visitor_hash = %s and type = %s
                 and created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'""",
            (project_id, vh, event_type),
        )).fetchone())["n"]
        if used >= limit:
            return CheckOut(allowed=False, reason="rate_limited", remaining_today=0)

        row = await (await c.execute(
            "insert into events (project_id, type, visitor_hash) values (%s, %s, %s) returning id",
            (project_id, event_type, vh),
        )).fetchone()
        return CheckOut(allowed=True, reason="ok", run_id=row["id"], remaining_today=limit - used - 1)


class EventIn(BaseModel):
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    model: str | None = Field(default=None, pattern=MODEL_RE)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0, le=100)
    latency_ms: int | None = Field(default=None, ge=0)
    score: float | None = None
    visitor_id: str | None = Field(default=None, max_length=200)  # hashed before storage
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def not_reserved(cls, v: str) -> str:
        if v in RESERVED_EVENT_TYPES:
            raise ValueError(f"'{v}' is written by /check, not by projects")
        return v


@app.post("/projects/{project_id}/events", status_code=201)
async def project_event(
    body: EventIn,
    project_id: str = Depends(require_project),
    settings: Settings = Depends(get_settings),
):
    vh = hash_visitor(body.visitor_id, settings) if body.visitor_id else None
    async with db.conn() as c:
        row = await (await c.execute(
            """insert into events (project_id, type, model, tokens_in, tokens_out, cost_usd,
                                   latency_ms, score, visitor_hash, meta)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (project_id, body.type, body.model, body.tokens_in, body.tokens_out, body.cost_usd,
             body.latency_ms, body.score, vh, Jsonb(body.meta)),
        )).fetchone()
    return {"id": row["id"]}


# ---------------------------------------------------------------------------
# Owner (Supabase JWT + allow-list)
# ---------------------------------------------------------------------------

class ConfigPatch(BaseModel):
    llm_mode: Literal["free", "paid", "off"] | None = None
    model_free: str | None = Field(default=None, pattern=MODEL_RE)
    model_paid: str | None = Field(default=None, pattern=MODEL_RE)
    voice_calls_enabled: bool | None = None
    voice_model: str | None = Field(default=None, pattern=MODEL_RE)
    daily_budget_usd: Decimal | None = Field(default=None, ge=0, le=1000)
    max_runs_per_visitor: int | None = Field(default=None, ge=0, le=1000)
    extra: dict[str, Any] | None = None  # merged into existing extra


@app.patch("/admin/projects/{project_id}/config")
async def admin_patch_config(project_id: str, body: ConfigPatch, _owner: str = Depends(require_owner)):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no changes")
    extra = changes.pop("extra", None)
    sets, params = [], []
    for k, v in changes.items():
        sets.append(f"{k} = %s")  # keys come from the pydantic model, never from the client
        params.append(v)
    if extra is not None:
        sets.append("extra = extra || %s")
        params.append(Jsonb(extra))
    sets.append("budget_tripped_at = null")  # an owner edit acknowledges a budget trip
    async with db.conn() as c:
        row = await (await c.execute(
            f"update project_config set {', '.join(sets)} where project_id = %s returning *",
            (*params, project_id),
        )).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown project")
    return row


class PowerIn(BaseModel):
    on: bool


@app.post("/admin/projects/{project_id}/power")
async def admin_power(
    project_id: str, body: PowerIn,
    _owner: str = Depends(require_owner),
    settings: Settings = Depends(get_settings),
):
    async with db.conn() as c:
        await _load_config(c, project_id)
        services = await (await c.execute(
            "select id from project_services where project_id = %s", (project_id,)
        )).fetchall()
        if not body.on:
            # Block new runs first, then stop the services.
            await c.execute("update project_config set enabled = false where project_id = %s", (project_id,))

    errors: dict[str, str] = {}
    if services:
        try:
            async with RenderClient(settings) as render:
                for s in services:
                    try:
                        await render.set_power(s["id"], body.on)
                    except RenderError as e:
                        errors[s["id"]] = str(e)
        except RenderError as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))

    async with db.conn() as c:
        ok_ids = [s["id"] for s in services if s["id"] not in errors]
        if ok_ids:
            await c.execute(
                "update project_services set last_known_status = %s where id = any(%s)",
                ("starting" if body.on else "suspended", ok_ids),
            )
        if body.on:
            await c.execute("update project_config set enabled = true where project_id = %s", (project_id,))
    return {"enabled": body.on, "services": len(services), "errors": errors}


@app.get("/admin/projects/{project_id}/services")
async def admin_services(
    project_id: str,
    _owner: str = Depends(require_owner),
    settings: Settings = Depends(get_settings),
):
    async with db.conn() as c:
        services = await (await c.execute(
            "select id, label, last_known_status from project_services where project_id = %s", (project_id,)
        )).fetchall()
    out = []
    async with RenderClient(settings) as render:
        for s in services:
            try:
                suspended, url = await render.status(s["id"])
                if suspended == "suspended":
                    new = "suspended"
                else:
                    new = "live" if await probe_health(url) else "starting"
            except RenderError:
                new = "unknown"
            out.append({**s, "last_known_status": new})
    async with db.conn() as c:
        for s in out:
            await c.execute("update project_services set last_known_status = %s where id = %s",
                            (s["last_known_status"], s["id"]))
    return out


@app.get("/admin/usage")
async def admin_usage(_owner: str = Depends(require_owner)):
    async with db.conn() as c:
        return await (await c.execute(
            """select p.id as project_id, c.daily_budget_usd, c.llm_mode, c.enabled, c.budget_tripped_at,
                 coalesce(sum(e.cost_usd) filter (where e.created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'), 0) as spend_today_usd,
                 coalesce(sum(e.cost_usd) filter (where e.created_at >= date_trunc('month', now() at time zone 'utc') at time zone 'utc'), 0) as spend_month_usd,
                 count(e.*) filter (where e.type = 'run_started' and e.created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc') as runs_today,
                 max(e.created_at) filter (where e.type = 'run_started') as last_run_at
               from projects p join project_config c on c.project_id = p.id
               left join events e on e.project_id = p.id
               group by p.id, c.project_id order by p.sort_order, p.id"""
        )).fetchall()


class KillSwitchIn(BaseModel):
    mode: Literal["free", "off"]


@app.post("/admin/kill-switch")
async def admin_kill_switch(body: KillSwitchIn, _owner: str = Depends(require_owner)):
    """Everything to Free (or Off) and all voice calls disabled. Does not touch Render power."""
    async with db.conn() as c:
        rows = await (await c.execute(
            """update project_config set voice_calls_enabled = false,
                 llm_mode = case when %s::text = 'off' or llm_mode = 'off' then 'off' else 'free' end
               returning project_id""",
            (body.mode,),
        )).fetchall()
    return {"updated": [r["project_id"] for r in rows]}


# ---------------------------------------------------------------------------
# Cron (shared secret): budget enforcement, run every 10–15 min
# ---------------------------------------------------------------------------

@app.post("/internal/enforce-budgets", dependencies=[Depends(require_cron)])
async def enforce_budgets():
    async with db.conn() as c:
        rows = await (await c.execute(
            """with spend as (
                 select project_id, sum(cost_usd) as today from events
                 where created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'
                 group by project_id)
               update project_config c
                  set llm_mode = case when c.extra->>'over_budget_mode' = 'off' then 'off'
                                      when c.llm_mode = 'off' then 'off' else 'free' end,
                      voice_calls_enabled = false,
                      budget_tripped_at = now()
                 from spend s
                where s.project_id = c.project_id
                  and s.today >= c.daily_budget_usd
                  and (c.llm_mode = 'paid' or c.voice_calls_enabled
                       or (c.extra->>'over_budget_mode' = 'off' and c.llm_mode <> 'off'))
               returning c.project_id, s.today as spend_today_usd, c.llm_mode"""
        )).fetchall()
    # TODO (Phase 5): notify the owner (email / Telegram) for each tripped project.
    return {"tripped": rows, "checked_at": datetime.now(timezone.utc).isoformat()}
