"""Контрактные тесты внутренней аутентификации master -> агент.

Запуск без pytest (в venv уже есть httpx):

    venv/bin/python tests/test_internal_auth.py

Проверяет:
- без X-Internal-Token -> 401;
- неверный токен -> 401;
- валидный токен + валидный X-User-Id -> не 401 (запрос прошёл auth);
- валидный токен + отсутствующий/битый X-User-Id -> 401;
- GET /health без токена -> 200 (публичный liveness);
- GET /health/ready без токена -> 401 (readiness под токеном).
"""

from __future__ import annotations

import asyncio
import os
import sys

# Запуск из корня проекта, чтобы импортировался пакет `app` и `main`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.core.config import settings
from main import app

TOKEN = settings.MASTER_INTERNAL_TOKEN or ""
VALID_USER = "00000000-0000-0000-0000-000000000001"


async def _request(method: str, path: str, headers=None, json=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers or {}, json=json)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
async def no_token_is_401():
    r = await _request("POST", "/v1/chat/completions")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


@case
async def wrong_token_is_401():
    r = await _request(
        "POST", "/v1/chat/completions",
        headers={"X-Internal-Token": "wrong-token", "X-User-Id": VALID_USER},
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


@case
async def valid_token_missing_user_is_401():
    r = await _request(
        "POST", "/v1/chat/completions",
        headers={"X-Internal-Token": TOKEN},
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


@case
async def valid_token_bad_user_is_401():
    r = await _request(
        "POST", "/v1/chat/completions",
        headers={"X-Internal-Token": TOKEN, "X-User-Id": "not-a-uuid"},
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


@case
async def valid_token_valid_user_passes_auth():
    # Пустое тело -> 400 (валидация бизнес-кода), а не 401: middleware пропустил.
    r = await _request(
        "POST", "/v1/chat/completions",
        headers={"X-Internal-Token": TOKEN, "X-User-Id": VALID_USER},
        json={},
    )
    assert r.status_code != 401, (
        f"auth middleware blocked a valid request: {r.status_code}"
    )


@case
async def health_is_public():
    r = await _request("GET", "/health")
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    assert r.json() == {"status": "ok"}


@case
async def health_ready_requires_token():
    r = await _request("GET", "/health/ready")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


async def main() -> None:
    failures = 0
    for fn in CASES:
        name = fn.__name__
        try:
            await fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{failures} failed")
        sys.exit(1)
    print("\nall passed")


if __name__ == "__main__":
    asyncio.run(main())
