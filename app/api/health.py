from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.db.database import AsyncSessionLocal

router = APIRouter(tags=["ops"])


def _qdrant_ok() -> bool:
    try:
        from app.core import retrieval
        return retrieval.qdrant_client.get_collections() is not None
    except Exception:
        return False


def _collection_ok() -> bool:
    try:
        from app.core import retrieval
        return retrieval.qdrant_client.collection_exists(retrieval.QDRANT_COLLECTION)
    except Exception:
        return False


def _ollama_ok() -> bool:
    try:
        from app.core.llm import _client
        _client.list()
        return True
    except Exception:
        return False


async def _postgres_ok() -> bool:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/health")
async def health() -> dict:
    postgres = await _postgres_ok()
    qdrant = await run_in_threadpool(_qdrant_ok)
    ollama = await run_in_threadpool(_ollama_ok)

    checks = {"postgres": postgres, "qdrant": qdrant, "ollama": ollama}
    return {"status": "ok" if all(checks.values()) else "degraded", **checks}


@router.get("/ready")
async def ready() -> dict:
    collection = await run_in_threadpool(_collection_ok)
    ollama = await run_in_threadpool(_ollama_ok)

    try:
        from app.core import retrieval
        bm25_ready = retrieval.bm25 is not None
    except Exception:
        bm25_ready = False

    ready_ = collection and ollama
    return {
        "status": "ready" if ready_ else "not_ready",
        "collection": collection,
        "ollama": ollama,
        "bm25": bm25_ready,
    }
