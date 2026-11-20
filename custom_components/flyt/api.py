"""Async client for Flyt MinSide (flytpass.no).

Login uses the Azure AD B2C "local account" (e-mail + password) flow that the
MinSide web page uses, with PKCE. BankID logins cannot be automated, and the
refresh tokens B2C issues to this (SPA) client expire 24 h after login, so the
client logs in again with e-mail/password whenever the refresh token runs out.

This module has no Home Assistant imports, so it can be tested standalone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    API_BASE,
    B2C_HOST,
    B2C_POLICY,
    B2C_TENANT,
    CLIENT_ID,
    ORIGIN,
    REDIRECT_URI,
    SCOPES,
)

_LOGGER = logging.getLogger(__name__)

_POLICY_BASE = f"{B2C_HOST}/{B2C_TENANT}/{B2C_POLICY}"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
_TIMEOUT = aiohttp.ClientTimeout(total=60)


class FlytError(Exception):
    """Generic Flyt error."""


class FlytAuthError(FlytError):
    """Wrong credentials or login flow failure."""


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_code(location: str) -> str:
    parsed = urlparse(location)
    params = parse_qs(parsed.fragment) or parse_qs(parsed.query)
    if "error" in params:
        raise FlytAuthError(
            f"{params['error'][0]}: {params.get('error_description', [''])[0]}"
        )
    if "code" not in params:
        raise FlytAuthError("Login did not return an authorization code")
    return params["code"][0]


class FlytClient:
    """Client for the Flyt self-service gateway."""

    def __init__(
        self, session: aiohttp.ClientSession, email: str, password: str
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._access_token: str | None = None
        self._access_expires = 0.0
        self._refresh_token: str | None = None
        self._refresh_expires = 0.0

    # ------------------------------------------------------------------ auth

    async def login(self) -> None:
        """Full e-mail/password login through Azure AD B2C."""
        verifier, challenge = _pkce_pair()
        params = {
            "client_id": CLIENT_ID,
            "scope": SCOPES,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "response_mode": "fragment",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": secrets.token_hex(16),
            "state": secrets.token_hex(16),
        }
        authorize_url = f"{_POLICY_BASE}/oauth2/v2.0/authorize?{urlencode(params)}"
        headers = {"User-Agent": _USER_AGENT}

        # B2C keeps the login state in cookies. Their names contain "|" and
        # ":", which aiohttp's cookie jar re-sends wrapped in quotes, and B2C
        # then answers "Bad Request". So cookies are tracked by hand and sent
        # verbatim. This also keeps them out of Home Assistant's shared session.
        cookies: dict[str, str] = {}

        def remember(resp: aiohttp.ClientResponse) -> None:
            for raw in resp.headers.getall("Set-Cookie", []):
                name, _, rest = raw.partition("=")
                value = rest.split(";", 1)[0]
                expired = "expires=thu, 01-jan-1970" in raw.lower()
                if value and not expired:
                    cookies[name.strip()] = value
                else:
                    cookies.pop(name.strip(), None)

        def with_cookies(extra: dict[str, str]) -> dict[str, str]:
            cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
            return {**headers, **extra, "Cookie": cookie}

        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(), timeout=_TIMEOUT
        ) as b2c:
            async with b2c.get(authorize_url, headers=headers) as resp:
                html = await resp.text()
                remember(resp)
                if resp.status != 200:
                    raise FlytError(f"Authorize page returned HTTP {resp.status}")

            match = re.search(r"var SETTINGS = (\{.*?\});", html, re.S)
            if not match:
                raise FlytError("Could not find SETTINGS on the B2C login page")
            settings = json.loads(match.group(1))
            csrf = settings["csrf"]
            trans_id = settings["transId"]
            tenant_path = settings.get("hosts", {}).get(
                "tenant", f"/{B2C_TENANT}/{B2C_POLICY}"
            )
            base = f"{B2C_HOST}{tenant_path}"

            async with b2c.post(
                f"{base}/SelfAsserted",
                params={"tx": trans_id, "p": B2C_POLICY},
                data={
                    "request_type": "RESPONSE",
                    "email": self._email,
                    "password": self._password,
                },
                headers=with_cookies(
                    {
                        "X-CSRF-TOKEN": csrf,
                        "X-Requested-With": "XMLHttpRequest",
                        "Origin": B2C_HOST,
                        "Referer": authorize_url,
                    }
                ),
            ) as resp:
                text = await resp.text()
                remember(resp)
            try:
                result = json.loads(text)
            except ValueError as err:
                raise FlytError(
                    f"Unexpected login response (HTTP {resp.status}): {text[:200]}"
                ) from err
            if str(result.get("status")) != "200":
                raise FlytAuthError(result.get("message") or "Login rejected")

            async with b2c.get(
                f"{base}/api/CombinedSigninAndSignup/confirmed",
                params={
                    "rememberMe": "false",
                    "csrf_token": csrf,
                    "tx": trans_id,
                    "p": B2C_POLICY,
                },
                headers=with_cookies({"Referer": authorize_url}),
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location")
                if resp.status not in (301, 302, 303) or not location:
                    raise FlytAuthError(
                        f"Login confirmation failed (HTTP {resp.status})"
                    )
            code = _extract_code(location)

        await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            }
        )
        _LOGGER.debug("Logged in to Flyt")

    async def _token_request(self, data: dict[str, str]) -> None:
        payload = {"client_id": CLIENT_ID, "scope": SCOPES, **data}
        async with self._session.post(
            f"{_POLICY_BASE}/oauth2/v2.0/token",
            data=payload,
            # SPA redirect URIs require a cross-origin token request
            headers={"Origin": ORIGIN, "User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or "access_token" not in body:
                raise FlytAuthError(
                    f"Token request failed: {body.get('error')} "
                    f"{body.get('error_description', '')[:200]}"
                )
        now = time.time()
        self._access_token = body["access_token"]
        self._access_expires = now + int(body.get("expires_in", 3600)) - 120
        if body.get("refresh_token"):
            self._refresh_token = body["refresh_token"]
            self._refresh_expires = (
                now + int(body.get("refresh_token_expires_in", 86400)) - 300
            )

    async def _ensure_token(self, force: bool = False) -> str:
        now = time.time()
        if not force and self._access_token and now < self._access_expires:
            return self._access_token
        if self._refresh_token and now < self._refresh_expires:
            try:
                await self._token_request(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                    }
                )
                return self._access_token  # type: ignore[return-value]
            except FlytError as err:
                _LOGGER.debug("Token refresh failed, logging in again: %s", err)
        await self.login()
        return self._access_token  # type: ignore[return-value]

    # ------------------------------------------------------------------- api

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        for attempt in range(2):
            token = await self._ensure_token(force=attempt > 0)
            async with self._session.post(
                f"{API_BASE}{path}",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Origin": ORIGIN,
                },
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 401 and attempt == 0:
                    continue
                if resp.status != 200:
                    raise FlytError(f"POST {path} returned HTTP {resp.status}")
                return await resp.json(content_type=None)
        raise FlytAuthError("Not authorized")

    async def get_customers(self) -> list[dict[str, Any]]:
        """Customers (private person / company) this login has access to."""
        return await self._post("/customers", {}) or []

    async def get_agreements(self, customer_id: str) -> list[dict[str, Any]]:
        """Agreements for a customer, each with its OBEs (bombrikker) and vehicles."""
        data = await self._post(
            f"/customer/{customer_id}/agreementsWithOBEsAndVehicles", {}
        )
        return (data or {}).get("results", [])

    async def get_passings(
        self,
        obe_numbers: list[str],
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page_size: int = 200,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Passings for the given OBEs, newest first."""
        if not obe_numbers:
            return []
        results: list[dict[str, Any]] = []
        for page in range(max_pages):
            body: dict[str, Any] = {
                "max": page_size,
                "offset": page * page_size,
                "obeNumbers": obe_numbers,
            }
            if date_from:
                body["dateFrom"] = _iso_utc(date_from)
            if date_to:
                body["dateTo"] = _iso_utc(date_to)
            data = await self._post("/obes/passings", body) or {}
            chunk = data.get("results", [])
            results.extend(chunk)
            if len(chunk) < page_size or len(results) >= data.get("totalResults", 0):
                break
        return results
