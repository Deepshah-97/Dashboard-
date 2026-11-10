"""Thin wrapper over the Render REST API. RENDER_API_KEY never leaves this process."""

import httpx

from .settings import Settings


class RenderError(RuntimeError):
    pass


class RenderClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.render_api_key:
            raise RenderError("RENDER_API_KEY is not configured")
        self._client = httpx.AsyncClient(
            base_url=settings.render_api_base,
            headers={"Authorization": f"Bearer {settings.render_api_key}", "Accept": "application/json"},
            timeout=15,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._client.aclose()

    async def set_power(self, service_id: str, on: bool) -> None:
        r = await self._client.post(f"/services/{service_id}/{'resume' if on else 'suspend'}")
        if r.status_code not in (200, 202):
            raise RenderError(f"{service_id}: Render returned {r.status_code}")

    async def status(self, service_id: str) -> tuple[str, str | None]:
        """Returns ('suspended' | 'not_suspended', public url or None)."""
        r = await self._client.get(f"/services/{service_id}")
        if r.status_code != 200:
            raise RenderError(f"{service_id}: Render returned {r.status_code}")
        body = r.json()
        return body.get("suspended", "not_suspended"), (body.get("serviceDetails") or {}).get("url")


async def probe_health(url: str | None, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """A service that answers HTTP at all has finished waking up."""
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5, transport=transport) as c:
            r = await c.get(url)
        return r.status_code < 500
    except httpx.HTTPError:
        return False
