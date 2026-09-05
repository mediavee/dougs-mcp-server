"""Async HTTP client for the Dougs internal API with automatic session login."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
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


def _error_detail(resp: httpx.Response) -> str:
    """Dougs puts a human-readable reason in x-user-message / x-message headers."""
    for header in ("x-user-message", "x-message"):
        message = resp.headers.get(header)
        if message:
            return message
    return resp.text[:200]


def _session_path(email: str) -> Path:
    """Per-account cookie cache, so restarting the server does not burn a login."""
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "dougs-mcp"
    return root / f"session-{hashlib.sha256(email.encode()).hexdigest()[:16]}.json"


class DougsError(Exception):
    """Generic API error surfaced to the caller."""


class DougsAuthError(DougsError):
    """Login failed (bad credentials, 2FA required, or Cloudflare block)."""


class DougsClient:
    """Thin wrapper around the Dougs API.

    Authentication is a session cookie set by POST /auth/api/login. The cookie
    is cached on disk and reused across restarts — Dougs rate-limits logins to
    25 per hour and per account — and we re-login transparently on a 401.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._http = httpx.AsyncClient(
            base_url=settings.dougs_base_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Origin": settings.dougs_base_url,
                "Referer": f"{settings.dougs_base_url}/app/",
            },
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )
        self._authenticated = False
        # Bumped on each successful login; lets concurrent 401s coalesce into one re-login.
        self._auth_gen = 0
        self._login_lock = asyncio.Lock()
        self._catalogs: dict[int, dict[int, dict[str, Any]]] = {}
        self._catalog_lock = asyncio.Lock()
        self._metrics: dict[int, dict[str, dict[str, Any]]] = {}
        self._metrics_lock = asyncio.Lock()
        self._session_path = _session_path(settings.dougs_email)
        self._load_session()

    def _load_session(self) -> None:
        try:
            cookies = json.loads(self._session_path.read_text())
        except (OSError, ValueError):
            return
        for name, value in cookies.items():
            self._http.cookies.set(name, value, domain=".dougs.fr")
        self._authenticated = bool(cookies)

    def _save_session(self) -> None:
        cookies = dict(self._http.cookies)
        if not cookies:
            return
        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: several server instances share this file.
            tmp = self._session_path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(cookies))
            tmp.chmod(0o600)
            tmp.replace(self._session_path)
        except OSError:  # a read-only cache dir must not break the server
            pass

    async def login(self) -> None:
        try:
            resp = await self._http.post(
                "/auth/api/login",
                json={
                    "email": self._s.dougs_email,
                    "password": self._s.dougs_password.get_secret_value(),
                },
            )
        except httpx.HTTPError as exc:  # network / TLS failures
            raise DougsAuthError(f"login request failed: {exc}") from exc

        if resp.status_code == 429:
            # Dougs drops the x-ratelimit-* headers once blocking, so we cannot
            # tell how long is left; retrying while blocked may keep it going.
            reset = resp.headers.get("x-ratelimit-reset")
            countdown = f" Retry in ~{int(reset) // 60} min." if reset else ""
            raise DougsAuthError(
                "login rate-limited by Dougs (25 logins per hour for this account)."
                f"{countdown} Wait rather than retry; the cached session normally "
                "avoids re-logging in at all."
            )
        if resp.status_code in (401, 403):
            raise DougsAuthError(
                f"login rejected (status {resp.status_code}): {_error_detail(resp)}. "
                "Check credentials, or Cloudflare/2FA blocking."
            )
        if resp.status_code >= 400:
            raise DougsAuthError(
                f"login failed with status {resp.status_code}: {_error_detail(resp)}"
            )

        self._authenticated = True
        self._auth_gen += 1
        self._save_session()

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
        elif "set-cookie" in resp.headers:  # rolling session: keep the fresh cookie
            self._save_session()
        if resp.status_code >= 400:
            raise DougsError(f"{method} {path} -> {resp.status_code}: {_error_detail(resp)}")
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
        if self._s.dougs_company_id is not None:
            return self._s.dougs_company_id
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

    async def category_catalog(self, company_id: int) -> dict[int, dict[str, Any]]:
        """Full category catalog keyed by id, fetched once per company and cached.

        Includes the hidden resolved variants (e.g. "... (Avec TVA)") that
        breakdowns point to through resolvedCategoryId.
        """
        cached = self._catalogs.get(company_id)
        if cached is not None:
            return cached
        async with self._catalog_lock:
            if company_id not in self._catalogs:
                cats = await self.get(
                    f"/companies/{company_id}/categories", params={"full": "true"}
                )
                self._catalogs[company_id] = {c["id"]: c for c in cats}
        return self._catalogs[company_id]

    async def category(self, company_id: int, category_id: int) -> dict[str, Any]:
        """Resolve an accounting category by id, from the cached catalog."""
        catalog = await self.category_catalog(company_id)
        cached = catalog.get(category_id)
        if cached is not None:
            return cached
        data = await self.get(f"/companies/{company_id}/categories/{category_id}")
        catalog[category_id] = data
        return data

    async def metric_catalog(self, company_id: int) -> dict[str, dict[str, Any]]:
        """Time-series catalog keyed by series name, fetched once and cached.

        Series ids are uuids; callers address them by their stable name
        (e.g. "accounting.chiffre-d-affaires").
        """
        cached = self._metrics.get(company_id)
        if cached is not None:
            return cached
        async with self._metrics_lock:
            if company_id not in self._metrics:
                series = await self.get(
                    f"/companies/{company_id}/stats/series", params={"namespace": "accounting"}
                )
                self._metrics[company_id] = {x["name"]: x for x in series}
        return self._metrics[company_id]

    async def operation(self, company_id: int, operation_id: int) -> dict[str, Any]:
        """Fetch a single operation with its breakdowns."""
        return await self.get(f"/companies/{company_id}/operations/{operation_id}")

    async def update_operation(
        self,
        company_id: int,
        operation: dict[str, Any],
        updated_breakdown: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a mutated operation.

        The API expects the whole operation back, plus the breakdown that
        changed under `updatedBreakdown` so the backend can recompute the
        derived fields (VAT, counterpart, resolved category) for that line.
        """
        payload = {**operation, "updatedBreakdown": updated_breakdown}
        return await self.post(
            f"/companies/{company_id}/operations/{operation['id']}", json=payload
        )

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
