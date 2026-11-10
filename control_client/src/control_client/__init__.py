"""control_client: lets a project backend obey the owner dashboard without a redeploy.

    from control_client import Control, ProjectPaused
    ctl = Control(project_id="recruit-ai")         # reads CONTROL_API_URL and PROJECT_TOKEN from env

    cfg = ctl.config()                              # cached 30s; falls back to last-known, then to safe defaults
    gate = ctl.check(visitor_id)                    # budget + rate limit; if not gate.allowed → serve demo mode
    resp = ctl.complete(messages, purpose="resume_parse", visitor_id=visitor_id)
    ctl.log("interview_scored", score=78, latency_ms=4200)

Rules: fail cheap, never fail expensive. If the Control API is unreachable the client uses the last
config it saw, or free mode with voice off. A free model that errors may fall back to another free
model (config.extra["free_fallbacks"]); it never falls back to a paid model.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

__all__ = ["Control", "Config", "CheckResult", "ProjectPaused", "SAFE_DEFAULT"]
log = logging.getLogger("control_client")


class ProjectPaused(RuntimeError):
    """Raised when the project must not spend: llm_mode is 'off' or the project is disabled."""


@dataclass(frozen=True)
class Config:
    project_id: str
    enabled: bool = True
    llm_mode: str = "free"
    model_free: str = "groq/llama-3.1-8b-instant"
    model_paid: str | None = None
    voice_calls_enabled: bool = False
    voice_model: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    stale: bool = False  # True when served from cache/defaults because the API was unreachable

    @property
    def model(self) -> str | None:
        if self.llm_mode == "paid" and not self.stale:
            return self.model_paid
        if self.llm_mode == "off":
            return None
        return self.model_free

    @classmethod
    def from_api(cls, d: dict) -> "Config":
        return cls(
            project_id=d["project_id"], enabled=d["enabled"], llm_mode=d["llm_mode"],
            model_free=d["model_free"], model_paid=d.get("model_paid"),
            voice_calls_enabled=d["voice_calls_enabled"], voice_model=d.get("voice_model"),
            extra=d.get("extra") or {},
        )


def SAFE_DEFAULT(project_id: str) -> Config:
    return Config(project_id=project_id, stale=True)


@dataclass(frozen=True)
class CheckResult:
    allowed: bool
    reason: str
    run_id: int | None = None
    remaining_today: int | None = None


class Control:
    def __init__(
        self,
        project_id: str,
        base_url: str | None = None,
        token: str | None = None,
        cache_ttl: float = 30.0,
        timeout: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        completion_fn: Callable[..., Any] | None = None,
        cost_fn: Callable[[Any], float] | None = None,
    ):
        self.project_id = project_id
        base_url = base_url or os.environ.get("CONTROL_API_URL", "")
        token = token or os.environ.get("PROJECT_TOKEN", "")
        if not base_url or not token:
            log.warning("CONTROL_API_URL / PROJECT_TOKEN not set; running on safe defaults")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") or "http://control-api.invalid",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )
        self._ttl = cache_ttl
        self._lock = threading.Lock()
        self._cfg: Config | None = None
        self._fetched_at = 0.0
        self._completion_fn = completion_fn
        self._cost_fn = cost_fn

    # -- config --------------------------------------------------------------

    def config(self, force: bool = False) -> Config:
        with self._lock:
            if not force and self._cfg and not self._cfg.stale and time.monotonic() - self._fetched_at < self._ttl:
                return self._cfg
            try:
                r = self._http.get(f"/projects/{self.project_id}/config")
                r.raise_for_status()
                self._cfg = Config.from_api(r.json())
                self._fetched_at = time.monotonic()
            except (httpx.HTTPError, KeyError, ValueError) as e:
                log.warning("control API unreachable (%s); using %s", e, "cached config" if self._cfg else "safe defaults")
                base = self._cfg or SAFE_DEFAULT(self.project_id)
                # Stale config may keep running, but never on a paid model or with voice on.
                self._cfg = Config(**{**base.__dict__, "stale": True, "voice_calls_enabled": False})
            return self._cfg

    # -- gate ----------------------------------------------------------------

    def check(self, visitor_id: str, action: str = "run") -> CheckResult:
        """Ask before an expensive action. On API failure: free runs allowed, voice calls refused."""
        try:
            r = self._http.post(f"/projects/{self.project_id}/check", json={"visitor_id": visitor_id, "action": action})
            r.raise_for_status()
            return CheckResult(**r.json())
        except (httpx.HTTPError, TypeError, ValueError) as e:
            log.warning("check failed (%s)", e)
            cfg = self.config()
            if action == "voice_call" or not cfg.enabled or cfg.llm_mode == "off":
                return CheckResult(allowed=False, reason="control_api_unreachable")
            return CheckResult(allowed=True, reason="degraded_free_only")

    # -- events --------------------------------------------------------------

    def log(self, type: str, **fields: Any) -> None:
        """Fire-and-forget usage event. Never raises into the request path."""
        try:
            self._http.post(f"/projects/{self.project_id}/events", json={"type": type, **fields}).raise_for_status()
        except httpx.HTTPError as e:
            log.warning("event %s not logged (%s)", type, e)

    # -- LLM -----------------------------------------------------------------

    def complete(self, messages: list[dict], purpose: str, visitor_id: str | None = None, **kwargs: Any) -> Any:
        """Route to the configured model via LiteLLM and log tokens + cost. Raises ProjectPaused when off."""
        cfg = self.config()
        if not cfg.enabled or cfg.model is None:
            raise ProjectPaused(f"{self.project_id} is paused (enabled={cfg.enabled}, llm_mode={cfg.llm_mode})")

        candidates = [cfg.model]
        if cfg.model == cfg.model_free:
            candidates += [m for m in cfg.extra.get("free_fallbacks", []) if m not in candidates]

        completion, cost_of = self._llm()
        last_err: Exception | None = None
        for model in candidates:
            started = time.monotonic()
            try:
                resp = completion(model=model, messages=messages, **kwargs)
            except Exception as e:  # provider errors / rate limits
                last_err = e
                self.log("llm_error", model=model, visitor_id=visitor_id,
                         meta={"purpose": purpose, "error": type(e).__name__})
                if cfg.llm_mode == "paid":
                    break  # paid failures are surfaced, not retried on other models
                continue
            usage = getattr(resp, "usage", None)
            try:
                cost = float(cost_of(resp) or 0)
            except Exception:
                cost = 0.0
            self.log(
                "llm_call", model=model, visitor_id=visitor_id,
                tokens_in=getattr(usage, "prompt_tokens", None),
                tokens_out=getattr(usage, "completion_tokens", None),
                cost_usd=round(cost, 6),
                latency_ms=int((time.monotonic() - started) * 1000),
                meta={"purpose": purpose},
            )
            return resp
        raise last_err or RuntimeError("no model available")

    def _llm(self):
        if self._completion_fn:
            return self._completion_fn, (self._cost_fn or (lambda _r: 0.0))
        import litellm  # imported lazily so the package installs without it

        return litellm.completion, (self._cost_fn or litellm.completion_cost)
