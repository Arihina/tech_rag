from __future__ import annotations

import logging
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("tech_rag.config")

MIN_PROD_TOKEN_LENGTH = 32


class Settings(BaseSettings):
    ENV: str = "dev"

    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASSWORD: str
    DB_NAME: str

    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6335
    QDRANT_GRPC_PORT: int = 6334
    QDRANT_PREFER_GRPC: bool = False
    QDRANT_API_KEY: str | None = None
    QDRANT_COLLECTION: str = "techdocs_hybrid"

    EMBED_MODEL: str = "BAAI/bge-m3"
    # Устройство эмбеддера ("cpu", "cuda", "cuda:0", "mps").
    # По умолчанию CPU: на 16 ГБ GPU живут Ollama и мастер-роутер, а bge-m3
    # кодирует один запрос на CPU за ~0.2-0.3 с — на фоне генерации это неважно.
    EMBED_DEVICE: str = "cpu"
    OLLAMA_MODEL: str = "qwen3.6:35b"
    OLLAMA_HOST: str = "http://localhost:11434"

    ALLOW_GENERAL_ANSWER: bool = False

    OLLAMA_NUM_CTX: int = 8192
    OLLAMA_NUM_PREDICT: int | None = None
    MAX_CONTEXT_CHARS: int = 12000
    HISTORY_MAX_CHARS: int = 4000

    HISTORY_LIMIT: int = 10

    # --- Внутренняя аутентификация master -> агент ---
    # "off" допустим только для локальной разработки/тестов.
    INTERNAL_AUTH_MODE: Literal["off", "token", "assertion"] = "token"
    # Per-agent секрет, который выдаёт master. Хранится только в env/secret.
    MASTER_INTERNAL_TOKEN: str | None = None
    # Для этапа 2 (assertion). Пока не используется.
    MASTER_ASSERTION_PUBLIC_KEY: str | None = None
    # Пути без внутренней аутентификации. Только liveness, без данных.
    PUBLIC_PATHS: tuple[str, ...] = ("/health",)
    AGENT_ID: str = "tech_rag"

    @model_validator(mode="after")
    def _validate_internal_auth(self) -> "Settings":
        if self.ENV == "prod" and self.INTERNAL_AUTH_MODE == "off":
            raise ValueError("INTERNAL_AUTH_MODE=off запрещён при ENV=prod")

        if self.INTERNAL_AUTH_MODE == "token":
            if not self.MASTER_INTERNAL_TOKEN:
                raise ValueError(
                    "INTERNAL_AUTH_MODE=token требует MASTER_INTERNAL_TOKEN "
                    "(fail-closed): задайте секрет, выданный master."
                )
            if (
                self.ENV == "prod"
                and len(self.MASTER_INTERNAL_TOKEN) < MIN_PROD_TOKEN_LENGTH
            ):
                raise ValueError(
                    f"MASTER_INTERNAL_TOKEN короче {MIN_PROD_TOKEN_LENGTH} "
                    "символов (ENV=prod)"
                )

        if (
            self.INTERNAL_AUTH_MODE == "assertion"
            and not self.MASTER_ASSERTION_PUBLIC_KEY
        ):
            raise ValueError(
                "INTERNAL_AUTH_MODE=assertion требует "
                "MASTER_ASSERTION_PUBLIC_KEY"
            )

        if self.INTERNAL_AUTH_MODE == "off":
            logger.warning(
                "INTERNAL_AUTH_MODE=off: входящие запросы НЕ проверяются. "
                "Допустимо только для локальной разработки/тестов."
            )
        return self

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def qdrant_url(self) -> str:
        return f"http://{self.QDRANT_HOST}:{self.QDRANT_PORT}"

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
