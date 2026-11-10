import hashlib
import json
import time
from decimal import Decimal

import httpx
import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

TOKEN = "pt_test_recruit"
OTHER_TOKEN = "pt_test_other"
JWT_SECRET = "test-jwt-secret-that-is-long-enough-for-hs256"
OWNER = "owner@example.com"


@pytest.fixture(scope="module")
def client(dsn, monkeypatch_module):
    monkeypatch_module.setenv("DATABASE_URL", dsn)
    monkeypatch_module.setenv("VISITOR_HASH_KEY", "k")
    monkeypatch_module.setenv("SUPABASE_JWT_SECRET", JWT_SECRET)
    monkeypatch_module.setenv("OWNER_EMAILS", json.dumps([OWNER]))
    monkeypatch_module.setenv("CRON_SECRET", "cron")
    monkeypatch_module.setenv("RENDER_API_KEY", "rnd_test")
    from app import settings as s
    s.get_settings.cache_clear()

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("insert into projects(id, name) values ('other-proj', 'Other') on conflict do nothing")
        for tok, pid in ((TOKEN, "recruit-ai"), (OTHER_TOKEN, "other-proj")):
            c.execute("insert into project_tokens values (%s, %s) on conflict do nothing",
                      (hashlib.sha256(tok.encode()).hexdigest(), pid))
        c.execute("update project_config set enabled = true, max_runs_per_visitor = 2 where project_id = 'recruit-ai'")

    from app.main import app
    with TestClient(app) as tc:
        yield tc


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def P(tok=TOKEN):
    return {"Authorization": f"Bearer {tok}"}


def owner_headers(email=OWNER):
    tok = jwt.encode({"email": email, "aud": "authenticated", "exp": int(time.time()) + 60}, JWT_SECRET, "HS256")
    return {"Authorization": f"Bearer {tok}"}


def test_config_requires_matching_token(client):
    assert client.get("/projects/recruit-ai/config").status_code == 401
    assert client.get("/projects/recruit-ai/config", headers=P(OTHER_TOKEN)).status_code == 403
    r = client.get("/projects/recruit-ai/config", headers=P())
    assert r.status_code == 200
    assert r.json()["llm_mode"] == "free" and r.json()["model"] == "groq/llama-3.1-8b-instant"
    assert "daily_budget_usd" not in r.json()


def test_check_rate_limits_per_visitor(client):
    results = [client.post("/projects/recruit-ai/check", headers=P(), json={"visitor_id": "v1"}).json() for _ in range(3)]
    assert [r["allowed"] for r in results] == [True, True, False]
    assert results[2]["reason"] == "rate_limited"
    # a different visitor still gets through
    assert client.post("/projects/recruit-ai/check", headers=P(), json={"visitor_id": "v2"}).json()["allowed"]


def test_voice_blocked_by_default(client):
    r = client.post("/projects/recruit-ai/check", headers=P(), json={"visitor_id": "v3", "action": "voice_call"})
    assert r.json() == {"allowed": False, "reason": "voice_disabled", "run_id": None, "remaining_today": None}


def test_events_hash_visitor_and_reject_reserved(client, dsn):
    r = client.post("/projects/recruit-ai/events", headers=P(),
                    json={"type": "resume_parsed", "score": 81, "latency_ms": 900, "visitor_id": "1.2.3.4"})
    assert r.status_code == 201
    with psycopg.connect(dsn) as c:
        vh = c.execute("select visitor_hash from events where id = %s", (r.json()["id"],)).fetchone()[0]
    assert vh and "1.2.3.4" not in vh
    assert client.post("/projects/recruit-ai/events", headers=P(), json={"type": "run_started"}).status_code == 422


def test_public_hides_private_fields(client):
    rows = client.get("/public/projects").json()
    rec = next(r for r in rows if r["id"] == "recruit-ai")
    assert rec["status"] == "live"  # enabled, service status not yet polled
    assert "daily_budget_usd" not in rec and "model_paid" not in rec
    assert rec["total_runs"] >= 3


def test_owner_auth(client):
    assert client.patch("/admin/projects/recruit-ai/config", json={"llm_mode": "paid"}).status_code == 401
    assert client.patch("/admin/projects/recruit-ai/config", json={"llm_mode": "paid"},
                        headers=owner_headers("x@example.com")).status_code == 403


def test_free_to_paid_takes_effect_without_redeploy(client):
    r = client.patch("/admin/projects/recruit-ai/config", headers=owner_headers(),
                     json={"llm_mode": "paid", "extra": {"score_threshold": 75}})
    assert r.status_code == 200
    cfg = client.get("/projects/recruit-ai/config", headers=P()).json()
    assert cfg["model"] == "openai/gpt-4o-mini" and cfg["extra"]["score_threshold"] == 75
    assert client.patch("/admin/projects/recruit-ai/config", headers=owner_headers(),
                        json={"llm_mode": "expensive"}).status_code == 422


def test_budget_trip(client):
    client.patch("/admin/projects/recruit-ai/config", headers=owner_headers(),
                 json={"llm_mode": "paid", "voice_calls_enabled": True, "daily_budget_usd": "0.05"})
    client.post("/projects/recruit-ai/events", headers=P(),
                json={"type": "llm_call", "model": "openai/gpt-4o-mini", "cost_usd": "0.06"})
    blocked = client.post("/projects/recruit-ai/check", headers=P(), json={"visitor_id": "v9"}).json()
    assert blocked == {"allowed": False, "reason": "over_budget", "run_id": None, "remaining_today": None}
    assert client.post("/internal/enforce-budgets").status_code == 401
    r = client.post("/internal/enforce-budgets", headers={"Authorization": "Bearer cron"}).json()
    assert [t["project_id"] for t in r["tripped"]] == ["recruit-ai"]
    cfg = client.get("/projects/recruit-ai/config", headers=P()).json()
    assert cfg["llm_mode"] == "free" and cfg["voice_calls_enabled"] is False
    usage = client.get("/admin/usage", headers=owner_headers()).json()
    rec = next(u for u in usage if u["project_id"] == "recruit-ai")
    assert round(Decimal(str(rec["spend_today_usd"])), 6) == Decimal("0.06") and rec["budget_tripped_at"]
    # reset budget for later tests
    client.patch("/admin/projects/recruit-ai/config", headers=owner_headers(), json={"daily_budget_usd": "100"})


def test_power_toggle_calls_render(client, monkeypatch):
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, req.headers["authorization"]))
        if req.method == "GET":
            return httpx.Response(200, json={"suspended": "not_suspended", "serviceDetails": {"url": "https://x.onrender.com"}})
        return httpx.Response(202)

    import app.main as m
    real = m.RenderClient
    monkeypatch.setattr(m, "RenderClient", lambda s: real(s, transport=httpx.MockTransport(handler)))

    async def healthy(url):
        return True
    monkeypatch.setattr(m, "probe_health", healthy)

    r = client.post("/admin/projects/recruit-ai/power", headers=owner_headers(), json={"on": False}).json()
    assert r == {"enabled": False, "services": 1, "errors": {}}
    assert calls[-1] == ("POST", "/v1/services/srv-REPLACE-BACKEND/suspend", "Bearer rnd_test")
    assert client.get("/public/projects/recruit-ai/status").json()["status"] == "paused"
    assert client.post("/projects/recruit-ai/check", headers=P(), json={"visitor_id": "v10"}).json()["reason"] == "disabled"

    client.post("/admin/projects/recruit-ai/power", headers=owner_headers(), json={"on": True})
    assert calls[-1][1].endswith("/resume")
    assert client.get("/public/projects/recruit-ai/status").json()["status"] == "waking"
    svc = client.get("/admin/projects/recruit-ai/services", headers=owner_headers()).json()
    assert svc[0]["last_known_status"] == "live"
    assert client.get("/public/projects/recruit-ai/status").json()["status"] == "live"


def test_kill_switch(client):
    client.patch("/admin/projects/recruit-ai/config", headers=owner_headers(), json={"llm_mode": "paid", "voice_calls_enabled": True})
    client.post("/admin/kill-switch", headers=owner_headers(), json={"mode": "free"})
    cfg = client.get("/projects/recruit-ai/config", headers=P()).json()
    assert cfg["llm_mode"] == "free" and not cfg["voice_calls_enabled"]
