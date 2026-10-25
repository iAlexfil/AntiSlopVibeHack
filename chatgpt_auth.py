"""Renew the local ChatGPT OAuth session without exposing credentials."""
from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import re
import time
from pathlib import Path

import httpx

from chatgpt_login import DIRECTORY, DIRECT_SCOPE, ISSUER, RESOURCE, save_json

LOGIN_AGAIN = "Выполните .venv/bin/python chatgpt_login.py для входа."
REQUIRED_SCOPES = {DIRECT_SCOPE, "offline_access", "resource.invoke"}


class AuthError(ValueError):
    """A safe authorization error that may be displayed to the user."""


def validate_credentials(credentials):
    if not isinstance(credentials, dict):
        raise ValueError
    if credentials.get("issuer") != ISSUER:
        raise ValueError
    for field in ("access_token", "refresh_token", "subject", "client_id", "scope"):
        if not isinstance(credentials.get(field), str) or not credentials[field]:
            raise ValueError
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", credentials["client_id"]) or credentials["client_id"] == "dynamic_agent_client":
        raise ValueError
    if not isinstance(credentials.get("token_type"), str) or credentials["token_type"].lower() != "bearer":
        raise ValueError
    if not REQUIRED_SCOPES.issubset(credentials["scope"].split()):
        raise ValueError
    expiry = credentials.get("expires_at")
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
        raise ValueError


class ChatGPTAuth:
    def __init__(self, credentials_path: Path, *, client: httpx.AsyncClient | None = None):
        self.credentials_path = credentials_path
        self._client = client or httpx.AsyncClient(timeout=30, trust_env=False)
        self._owns_client = client is None
        self._lock = asyncio.Lock()

    async def access_token(self) -> str:
        async with self._lock:
            try:
                descriptor = os.open(self.credentials_path.parent / ".login.lock", os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(descriptor, "w") as lock:
                    while True:
                        try:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            await asyncio.sleep(0.05)
                    try:
                        credentials = json.loads(self.credentials_path.read_text())
                        validate_credentials(credentials)
                    except (OSError, ValueError, TypeError, OverflowError):
                        raise AuthError("Сессия ChatGPT повреждена или недоступна. " + LOGIN_AGAIN) from None
                    if credentials["expires_at"] > time.time() + 60:
                        return credentials["access_token"]
                    return await self._finish_refresh(credentials)
            except OSError:
                raise AuthError("Не удалось прочитать или сохранить сессию ChatGPT. Проверьте права на хранилище. " + LOGIN_AGAIN) from None

    async def _finish_refresh(self, credentials):
        # A cancelled request must still save a rotated refresh token before shutdown.
        task = asyncio.create_task(self._refresh(credentials))
        cancelled = False
        while True:
            try:
                token = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError
        return token

    async def _refresh(self, credentials):
        try:
            response = await self._client.post(
                ISSUER + "/api/accounts/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": credentials["client_id"],
                    "refresh_token": credentials["refresh_token"],
                    "resource": RESOURCE,
                },
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise AuthError("Не удалось обновить сессию ChatGPT из-за сетевой ошибки. Повторите позже.") from None
        received_at = time.time()
        if response.status_code != 200:
            raise AuthError(f"Не удалось обновить сессию ChatGPT (HTTP {response.status_code}). " + LOGIN_AGAIN) from None
        try:
            if len(response.content) > 262144:
                raise ValueError
            tokens = response.json()
            if not isinstance(tokens, dict) or type(tokens.get("expires_in")) is not int or tokens["expires_in"] <= 0:
                raise ValueError
            for field in ("access_token", "refresh_token", "token_type"):
                if not isinstance(tokens.get(field), str) or not tokens[field]:
                    raise ValueError
            scope = tokens.get("scope", credentials["scope"])
            refreshed = {
                **credentials,
                **{field: tokens[field] for field in ("access_token", "refresh_token", "token_type", "expires_in")},
                "scope": scope,
                "scopes": scope.split() if isinstance(scope, str) else [],
                "saved_at": received_at,
                "expires_at": received_at + tokens["expires_in"],
            }
            validate_credentials(refreshed)
        except (ValueError, TypeError, OverflowError):
            raise AuthError("OpenAI вернул некорректную сессию ChatGPT. " + LOGIN_AGAIN) from None
        save_json(self.credentials_path, refreshed)
        return refreshed["access_token"]

    async def close(self):
        async with self._lock:
            if self._owns_client:
                await self._client.aclose()


def status() -> dict:
    """Local status only: never refresh or return credentials to the browser."""
    configured = False
    try:
        credentials = json.loads((DIRECTORY / "credentials.json").read_text())
        validate_credentials(credentials)
        configured = True
    except (OSError, ValueError, TypeError, OverflowError):
        pass
    return {
        "configured": configured,
        "mode": "chatgpt_plan",
        "model": os.getenv("CHATGPT_VISION_MODEL", "gpt-6-luna"),
        "detail": "Вход через ChatGPT подключён." if configured else LOGIN_AGAIN,
    }
