"""Внутренняя аутентификация master -> агент (vendored platform_security).

Контракт master -> агент (см. master_node/docs/agent-side-changes.md):

* ``X-Internal-Token`` — per-agent секрет, доказывает, что запрос пришёл от
  master (обязательный);
* ``X-User-Id`` — UUID пользователя из проверенного JWT (обязательный);
* ``X-Request-Id`` — id запроса для сквозной корреляции логов (служебный).

Middleware закрывает по умолчанию все пути, кроме ``PUBLIC_PATHS``
(точное совпадение, не ``startswith``). Проверка токена — ``hmac.compare_digest``
(constant-time). ``user_id`` и ``request_id`` кладутся в ``request.state``;
бизнес-код читает их только оттуда, а не напрямую из заголовков.
"""

from __future__ import annotations

import hmac
import logging
import re
import time
import uuid
from uuid import UUID

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("tech_rag.internal_auth")

INTERNAL_TOKEN_HEADER = "X-Internal-Token"
USER_ID_HEADER = "X-User-Id"
REQUEST_ID_HEADER = "X-Request-Id"

# Значение X-Request-Id принимается от master/nginx только в этом виде.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def normalize_request_id(value: str | None) -> str:
    if value and _REQUEST_ID_RE.match(value):
        return value
    return new_request_id()


def _safe_log_value(value: object, limit: int = 512) -> str:
    text = str(value)[:limit]
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def _auth_error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {
            "message": message,
            "type": "authentication_error",
            "param": None,
            "code": None,
        }},
    )


class InternalAuthMiddleware:
    """Чистый ASGI-middleware (не BaseHTTPMiddleware — тот буферизует тело и
    ломает SSE-стриминг ответов)."""

    def __init__(self, app: ASGIApp, settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        state = scope.setdefault("state", {})
        headers = Headers(scope=scope)

        request_id = normalize_request_id(headers.get(REQUEST_ID_HEADER))
        state["request_id"] = request_id

        user_id: UUID | None = None

        if path not in self.settings.PUBLIC_PATHS:
            if self.settings.INTERNAL_AUTH_MODE != "off":
                expected = self.settings.MASTER_INTERNAL_TOKEN or ""
                presented = headers.get(INTERNAL_TOKEN_HEADER) or ""
                if not expected or not hmac.compare_digest(presented, expected):
                    await self._reject(
                        scope, receive, send, request_id,
                        "Требуется внутренний токен master",
                    )
                    return

            raw_user = headers.get(USER_ID_HEADER)
            if raw_user is None:
                await self._reject(
                    scope, receive, send, request_id,
                    "Не передан идентификатор пользователя",
                )
                return
            try:
                user_id = UUID(raw_user.strip())
            except (ValueError, AttributeError):
                await self._reject(
                    scope, receive, send, request_id,
                    "Некорректный идентификатор пользователя",
                )
                return
            state["user_id"] = str(user_id)

        await self._run(scope, receive, send, request_id, user_id)

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send,
        request_id: str, message: str,
    ) -> None:
        logger.info(
            "request_id=%s agent_id=%s method=%s path=%s status=401 "
            "user_id=- auth=rejected reason=%s",
            request_id,
            self.settings.AGENT_ID,
            _safe_log_value(scope.get("method", "")),
            _safe_log_value(scope.get("path", "")),
            _safe_log_value(message),
        )

        async def send_with_id(message_: Message) -> None:
            if message_["type"] == "http.response.start":
                MutableHeaders(scope=message_)[REQUEST_ID_HEADER] = request_id
            await send(message_)

        await _auth_error(message)(scope, receive, send_with_id)

    async def _run(
        self, scope: Scope, receive: Receive, send: Send,
        request_id: str, user_id: UUID | None,
    ) -> None:
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "request_id=%s agent_id=%s method=%s path=%s status=%d "
                "user_id=%s duration_ms=%.1f",
                request_id,
                self.settings.AGENT_ID,
                _safe_log_value(scope.get("method", "")),
                _safe_log_value(scope.get("path", "")),
                status,
                str(user_id) if user_id else "-",
                duration_ms,
            )


def install_internal_auth(app: ASGIApp, settings) -> None:
    """Подключить проверку входящих запросов ДО включения роутеров."""
    app.add_middleware(InternalAuthMiddleware, settings=settings)
