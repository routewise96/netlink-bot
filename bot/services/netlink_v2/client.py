"""Async Control Plane internal-API client for the Telegram bot.

Transport is injectable for tests. The production transport uses aiohttp
(imported lazily) with bounded timeouts:

    connect 3s, socket-read 5s, total 10s

Retry policy:
- GET employee: exactly one bounded retry on transport-level transient errors;
- POST employee / enrollment: never retried automatically (no idempotency
  contract on the server side for unknown outcomes).

Secrets (internal API key, enrollment codes) never appear in exceptions or
logs produced here.
"""
from __future__ import annotations

from typing import Any, Protocol


class ControlPlaneError(Exception):
    """Base class; messages are safe for logs (no secrets, no bodies)."""


class ControlPlaneUnavailable(ControlPlaneError):
    """Network/timeout/5xx: the Control Plane could not be reached or failed."""


class ControlPlaneUnauthorized(ControlPlaneError):
    """401/403: internal API key invalid (bot-side configuration problem)."""


class ControlPlaneConflict(ControlPlaneError):
    """409: domain-level conflict (e.g. employee already exists)."""


class EmployeeNotFound(ControlPlaneError):
    """Stable 404 from the by-telegram lookup."""


class UnexpectedResponse(ControlPlaneError):
    """Any other non-success response."""


class Transport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]: ...

    async def close(self) -> None: ...


class AiohttpTransport:
    """Production transport over aiohttp with bounded timeouts."""

    def __init__(self, base_url: str) -> None:
        import aiohttp

        self._session = aiohttp.ClientSession(
            base_url=base_url,
            timeout=aiohttp.ClientTimeout(connect=3, sock_read=5, total=10),
            raise_for_status=False,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        try:
            async with self._session.request(
                method, path, headers=headers, json=json_body
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 - non-JSON body
                    body = None
                return response.status, body
        except Exception as exc:  # aiohttp.ClientError, asyncio.TimeoutError
            raise ControlPlaneUnavailable(
                f"control plane transport error: {type(exc).__name__}"
            ) from None

    async def close(self) -> None:
        await self._session.close()


def _map_status_error(status: int, code: str) -> ControlPlaneError:
    if status >= 500:
        return ControlPlaneUnavailable(f"control plane server error ({status})")
    if status in (401, 403):
        return ControlPlaneUnauthorized("control plane rejected the internal api key")
    if status == 409:
        return ControlPlaneConflict(f"control plane conflict: {code}")
    if status == 404:
        return UnexpectedResponse(f"unexpected 404: {code}")
    return UnexpectedResponse(f"unexpected response status {status}: {code}")


class _RequestFailed(Exception):
    """Internal carrier for a non-success response (status, error code)."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


def _raise_mapped(status: int, code: str) -> None:
    raise _map_status_error(status, code)


class ControlPlaneClient:
    def __init__(
        self,
        *,
        base_url: str,
        internal_api_key: str,
        transport: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key = internal_api_key
        self._transport = transport if transport is not None else AiohttpTransport(self._base_url)
        self._headers = {"X-Internal-Api-Key": internal_api_key}

    async def close(self) -> None:
        await self._transport.close()

    async def _request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> Any:
        status, body = await self._transport.request(
            method, path, headers=self._headers, json_body=json_body
        )
        if status >= 400:
            code = "unknown"
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict) and error.get("code"):
                    code = str(error["code"])
            raise _RequestFailed(status, code)
        return body

    async def get_employee_by_telegram(self, telegram_user_id: int) -> dict[str, Any]:
        path = f"/v1/employees/by-telegram/{telegram_user_id}"
        body: Any = None
        for attempt in (1, 2):
            try:
                body = await self._request("GET", path)
                break
            except _RequestFailed as exc:
                if exc.status == 404:
                    raise EmployeeNotFound("employee not found") from None
                _raise_mapped(exc.status, exc.code)
            except ControlPlaneUnavailable:
                # one bounded retry for transport-level transient errors only
                if attempt == 2:
                    raise
        if not isinstance(body, dict):
            raise UnexpectedResponse("employee lookup returned non-object body") from None
        return body

    async def create_employee(
        self, telegram_user_id: int, full_name: str
    ) -> dict[str, Any]:
        try:
            body = await self._request(
                "POST",
                "/v1/employees",
                json_body={"telegram_user_id": telegram_user_id, "full_name": full_name},
            )
        except _RequestFailed as exc:
            _raise_mapped(exc.status, exc.code)
        if not isinstance(body, dict):
            raise UnexpectedResponse("employee create returned non-object body") from None
        return body

    async def create_enrollment(self, employee_id: str) -> dict[str, Any]:
        try:
            body = await self._request(
                "POST", "/v1/enrollments", json_body={"employee_id": employee_id}
            )
        except _RequestFailed as exc:
            _raise_mapped(exc.status, exc.code)
        if not isinstance(body, dict):
            raise UnexpectedResponse("enrollment create returned non-object body") from None
        return body


__all__ = [
    "AiohttpTransport",
    "ControlPlaneClient",
    "ControlPlaneConflict",
    "ControlPlaneError",
    "ControlPlaneUnavailable",
    "ControlPlaneUnauthorized",
    "EmployeeNotFound",
    "UnexpectedResponse",
]
