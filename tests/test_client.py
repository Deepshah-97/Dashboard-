import httpx
import pytest

from control_client import Control, ProjectPaused


class FakeResp:
    class usage:
        prompt_tokens = 10
        completion_tokens = 5


def api(cfg, events, check=None):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/config"):
            return httpx.Response(200, json=cfg)
        if req.url.path.endswith("/events"):
            import json
            events.append(json.loads(req.content))
            return httpx.Response(201, json={"id": 1})
        if req.url.path.endswith("/check"):
            return httpx.Response(200, json=check or {"allowed": True, "reason": "ok", "run_id": 1, "remaining_today": 2})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def CFG(**kw):
    base = {"project_id": "recruit-ai", "enabled": True, "llm_mode": "free", "model_free": "groq/a",
            "model_paid": "openai/b", "voice_calls_enabled": False, "voice_model": None, "extra": {}}
    return {**base, **kw}


def test_routes_to_paid_and_logs_cost():
    events, used = [], []
    ctl = Control("recruit-ai", "http://api", "t", transport=api(CFG(llm_mode="paid"), events),
                  completion_fn=lambda model, messages: used.append(model) or FakeResp(), cost_fn=lambda r: 0.0012)
    ctl.complete([{"role": "user", "content": "hi"}], purpose="resume_parse", visitor_id="v")
    assert used == ["openai/b"]
    assert events[0]["type"] == "llm_call" and events[0]["cost_usd"] == 0.0012 and events[0]["tokens_in"] == 10


def test_off_raises_paused():
    ctl = Control("recruit-ai", "http://api", "t", transport=api(CFG(llm_mode="off"), []), completion_fn=lambda **k: None)
    with pytest.raises(ProjectPaused):
        ctl.complete([], purpose="x")


def test_free_falls_back_to_free_never_paid():
    used = []

    def flaky(model, messages):
        used.append(model)
        if model == "groq/a":
            raise RuntimeError("rate limited")
        return FakeResp()

    ctl = Control("recruit-ai", "http://api", "t",
                  transport=api(CFG(extra={"free_fallbacks": ["gemini/c"]}), []), completion_fn=flaky)
    ctl.complete([], purpose="x")
    assert used == ["groq/a", "gemini/c"]


def test_unreachable_api_fails_cheap():
    down = httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("down")))
    ctl = Control("recruit-ai", "http://api", "t", transport=down, completion_fn=lambda **k: FakeResp())
    cfg = ctl.config()
    assert cfg.stale and cfg.model == "groq/llama-3.1-8b-instant" and not cfg.voice_calls_enabled
    assert ctl.check("v").allowed is True
    assert ctl.check("v", action="voice_call").allowed is False


def test_cached_paid_config_degrades_to_free_when_api_drops():
    state = {"up": True}

    def handler(req):
        if not state["up"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=CFG(llm_mode="paid", voice_calls_enabled=True))

    ctl = Control("recruit-ai", "http://api", "t", transport=httpx.MockTransport(handler), cache_ttl=0)
    assert ctl.config().model == "openai/b"
    state["up"] = False
    cfg = ctl.config()
    assert cfg.model == "groq/a" and not cfg.voice_calls_enabled
