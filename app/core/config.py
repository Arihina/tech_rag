from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASSWORD: str
    DB_NAME: str

    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_GRPC_PORT: int = 6334
    QDRANT_PREFER_GRPC: bool = False
    QDRANT_API_KEY: str | None = None
    QDRANT_COLLECTION: str = "techdocs_hybrid"

    EMBED_MODEL: str = "BAAI/bge-m3"
    OLLAMA_MODEL: str = "qwen3.6:35b"
    OLLAMA_HOST: str = "http://localhost:11434"

    HISTORY_LIMIT: int = 10

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
