from uuid import UUID

from fastapi import HTTPException, Request


async def get_user_id(request: Request) -> UUID:
    """Единственная точка получения user_id для бизнес-кода.

    Значение кладёт `InternalAuthMiddleware` в `request.state` после проверки
    `X-Internal-Token`; напрямую из заголовка `X-User-Id` читать нельзя —
    иначе легко забыть проверку на новой ручке.
    """
    raw = getattr(request.state, "user_id", None)
    if raw is None:
        raise HTTPException(401, "Не передан идентификатор пользователя")
    try:
        return UUID(str(raw))
    except ValueError:
        raise HTTPException(401, "Некорректный идентификатор пользователя")
