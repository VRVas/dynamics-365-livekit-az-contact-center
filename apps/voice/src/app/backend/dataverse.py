"""Shared authenticated Dataverse transport for native voice integrations."""

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp
from azure.core.exceptions import AzureError

from backend.azure import get_azure_credentials

logger = logging.getLogger("dataverse")


class DataverseError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400, *, retry_after: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after


class DataverseClient:
    def __init__(self, org_url: str, tenant_id: str | None = None, *, timeout: int = 25):
        self.org_url = org_url
        self.tenant_id = tenant_id
        self.timeout = timeout
        self._credential = None
        self._http: aiohttp.ClientSession | None = None

    async def close(self):
        if self._http is not None:
            await self._http.close()
        if self._credential is not None:
            await asyncio.to_thread(self._credential.close)

    async def _request(
        self, method: str, path: str, *, body: dict | None = None, params: dict | None = None,
        etag: str | None = None,
    ) -> dict[str, Any]:
        if not self.org_url:
            raise DataverseError("not_configured", "The Dataverse organization is not configured.", 503)
        try:
            if self._credential is None:
                self._credential = get_azure_credentials(self.tenant_id, warmup_scope=None)
            token = await asyncio.to_thread(self._credential.get_token, f"{self.org_url}/.default")
        except AzureError as error:
            logger.error("Dataverse token acquisition failed (%s)", type(error).__name__)
            raise DataverseError(
                "dynamics_authentication_failed", "Could not obtain a Dataverse token for the application identity.", 503,
            ) from error
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout, connect=5))
        try:
            async with self._http.request(
                method, f"{self.org_url}/api/data/v9.2/{path}",
                headers={
                    "Authorization": f"Bearer {token.token}", "Accept": "application/json",
                    "OData-Version": "4.0", "OData-MaxVersion": "4.0",
                    "Prefer": 'odata.include-annotations="OData.Community.Display.V1.FormattedValue"',
                    **({"If-Match": etag, "Prefer": "return=representation"} if etag else {}),
                },
                json=body, params=params, allow_redirects=False,
            ) as response:
                raw = await response.read()
                if not 200 <= response.status < 300:
                    logger.error("Dynamics %s failed: HTTP %s", path.split("(", 1)[0], response.status)
                    if response.status in {401, 403}:
                        raise DataverseError(
                            "dynamics_access_denied",
                            "Dynamics denied the application identity. Check its tenant and Dataverse role.", 502,
                        )
                    if response.status == 404:
                        raise DataverseError("dynamics_not_found", "The requested Dynamics record or operation was not found.", 404)
                    if response.status == 412:
                        raise DataverseError("dynamics_conflict", "The command changed before its conditional update.", 409)
                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After", "")
                        raise DataverseError(
                            "dynamics_throttled", "Dynamics throttled this request. Retry the same logical request ID.", 429,
                            retry_after=retry_after if re.fullmatch(r"\d{1,6}", retry_after) else None,
                        )
                    message = "Dynamics rejected the operation. Check the engagement configuration and request."
                    if response.status >= 500:
                        message = "Dynamics could not confirm the outcome. Do not create a replacement request ID."
                    raise DataverseError("dynamics_request_failed", message, 502)
                if response.status == 204 or not raw.strip():
                    return {}
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as error:
                    raise DataverseError("invalid_dynamics_response", "Dynamics returned an invalid response; the outcome is unconfirmed.", 502) from error
                if not isinstance(payload, dict):
                    raise DataverseError("invalid_dynamics_response", "Dynamics returned an unexpected response shape.", 502)
                return payload
        except (aiohttp.ClientError, TimeoutError) as error:
            logger.error("Dynamics transport failed (%s)", type(error).__name__)
            raise DataverseError(
                "dynamics_outcome_unknown",
                "The Dynamics response was not received. A submitted request may exist; retry only with the same request_id.",
                504,
            ) from error

    async def get(self, path: str, *, params: dict | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def update_native_command(self, command_id: str, values: dict, etag: str) -> dict[str, Any]:
        from backend.native_supervision import RESULT_FIELDS, TABLE, identifier

        command_id = identifier(command_id)
        if not values or set(values) - RESULT_FIELDS or not re.fullmatch(r'W/"\d+"', etag):
            raise ValueError("Only secured native command outcomes may be conditionally updated.")
        return await self._request("PATCH", f"{TABLE}({command_id})", body=values, etag=etag)
