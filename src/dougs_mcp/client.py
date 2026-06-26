"""Async HTTP client for the Dougs internal API with automatic session login."""

import asyncio
from typing import Any

import httpx

from .config import Settings

# The operations endpoint caps a single page at 500 rows server-side.
MAX_PAGE = 500

# Realistic UA to reduce the chance of a Cloudflare bot challenge on /auth/api/login.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


class DougsError(Exception):
    """Generic API error surfaced to the caller."""


class DougsAuthError(DougsError):
    """Login failed (bad credentials, 2FA required, or Cloudflare block)."""


class DougsClient:
    """Thin wrapper around the Dougs API.

    Authentication is a session cookie set by POST /auth/api/login. httpx keeps
    it in its cookie jar; we re-login transparently when the session expires.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Origin": settings.base_url,
                "Referer": f"{settings.base_url}/app/",
            },
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )
        self._authenticated = False
        # Bumped on each successful login; lets concurrent 401s coalesce into one re-login.
        self._auth_gen = 0
        self._login_lock = asyncio.Lock()
        self._categories: dict[int, dict[str, Any]] = {}

    async def login(self) -> None:
        try:
            resp = await self._http.post(
                "/auth/api/login",
                json={"email": self._s.email, "password": self._s.password},
            )
        except httpx.HTTPError as exc:  # network / TLS failures
            raise DougsAuthError(f"login request failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise DougsAuthError(
                "login rejected (status "
                f"{resp.status_code}). Check credentials, or Cloudflare/2FA blocking."
            )
        if resp.status_code >= 400:
            raise DougsAuthError(f"login failed with status {resp.status_code}")

        self._authenticated = True
        self._auth_gen += 1

    async def _ensure_auth(self) -> None:
        if self._authenticated:
            return
        async with self._login_lock:
            if not self._authenticated:  # double-check: another coroutine may have logged in
                await self.login()

    async def _reauth(self, stale_gen: int) -> None:
        """Re-login once for a burst of concurrent 401s sharing the same auth generation."""
        async with self._login_lock:
            if self._auth_gen == stale_gen:  # first one in re-logs; the rest are no-ops
                self._authenticated = False
                await self.login()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Authenticated request with a single transparent re-login on 401."""
        await self._ensure_auth()
        gen = self._auth_gen
        resp = await self._http.request(method, path, **kwargs)
        if resp.status_code == 401:
            await self._reauth(gen)
            resp = await self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise DougsError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp

    @staticmethod
    def _json_or_none(resp: httpx.Response) -> Any:
        return resp.json() if resp.content else None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Authenticated GET returning parsed JSON (None on an empty body)."""
        resp = await self._send("GET", path, params=params)
        return self._json_or_none(resp)

    async def post(
        self,
        path: str,
        json: Any = None,
        files: Any = None,
        data: Any = None,
    ) -> Any:
        """Authenticated POST (JSON or multipart) returning parsed JSON if any."""
        return self._json_or_none(await self._send("POST", path, json=json, files=files, data=data))

    async def delete(self, path: str) -> Any:
        """Authenticated DELETE returning parsed JSON if any."""
        return self._json_or_none(await self._send("DELETE", path))

    async def resolve_company_id(self, override: int | None = None) -> int:
        """Pick the company id: explicit arg > configured > user's preferred > first."""
        if override is not None:
            return override
        if self._s.company_id is not None:
            return self._s.company_id
        me = await self.get("/users/me")
        preferred = me.get("preferredCompanyId")
        if preferred:
            return int(preferred)
        companies = await self.get("/users/me/companies")
        if companies:
            return int(companies[0]["id"])
        raise DougsError("no company found for this account")

    async def active_accounting_year_id(self, company_id: int) -> int:
        """Return the id of the company's currently active accounting year."""
        year = await self.get(f"/companies/{company_id}/accounting-years/active")
        return int(year["id"])

    async def category(self, company_id: int, category_id: int) -> dict[str, Any]:
        """Resolve an accounting category by id (cached; the catalog is stable)."""
        cached = self._categories.get(category_id)
        if cached is not None:
            return cached
        data = await self.get(f"/companies/{company_id}/categories/{category_id}")
        self._categories[category_id] = data
        return data

    async def resolve_file_url(self, path: str) -> str:
        """Resolve a Dougs file path (e.g. '/files/...') to its direct S3 URL.

        Hitting the path with the session cookie returns a 302 to a signed S3 URL.
        """
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        await self._ensure_auth()
        gen = self._auth_gen
        resp = await self._http.get(path, follow_redirects=False)
        if resp.status_code == 401:
            await self._reauth(gen)
            resp = await self._http.get(path, follow_redirects=False)
        if resp.is_redirect:
            location = resp.headers.get("location")
            if location:
                return location
        if resp.status_code >= 400:
            raise DougsError(f"GET {path} -> {resp.status_code}")
        return str(resp.url)

    async def aclose(self) -> None:
        await self._http.aclose()
