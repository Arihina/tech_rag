from uuid import UUID
from fastapi import Header, HTTPException


async def get_user_id(x_user_id: str | None = Header(default=None)) -> UUID:
    if x_user_id is None:
        raise HTTPException(401, "Не передан идентификатор пользователя")
    try:
        return UUID(x_user_id)
    except ValueError:
        raise HTTPException(401, "Некорректный идентификатор пользователя")
