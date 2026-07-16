"""
Индексирует техническую документацию (Markdown / .txt) в Qdrant.

Использование:
    python3 -m ingestion.index_qdrant \
        --input_dir ./docs \
        --collection techdocs_hybrid \
        [--model BAAI/bge-m3] \
        [--chunk_size 800] \
        [--chunk_overlap 80] \
        [--reset]
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

from app.core.config import settings
from chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_OVERLAP,
    build_chunks,
    parse_file,
)

PASSAGE_PREFIX = ""

DOC_EXTENSIONS = {".md", ".markdown", ".txt"}


def _iter_doc_files(input_dir: Path):
    for path in sorted(input_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in DOC_EXTENSIONS:
            yield path


def _ensure_collection(
    client: QdrantClient,
    collection_name: str,
    vector_size: int,
    reset: bool,
) -> None:
    exists = client.collection_exists(collection_name)

    if reset and exists:
        client.delete_collection(collection_name)
        print(f"Коллекция '{collection_name}' удалена (--reset)")
        exists = False

    if not exists:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=vector_size,
                distance=models.Distance.COSINE,
            ),
        )
        print(f"Коллекция '{collection_name}' создана "
              f"(size={vector_size}, distance=COSINE)")


def _upsert_chunks(
    client: QdrantClient,
    collection_name: str,
    chunks: list[dict],
    source_name: str,
    embedder: SentenceTransformer,
) -> int:
    if not chunks:
        return 0

    texts = [PASSAGE_PREFIX + c["text"] for c in chunks]

    embeddings = []
    for i in range(0, len(texts), 64):
        batch = texts[i: i + 64]
        embeddings.extend(
            embedder.encode(batch, normalize_embeddings=True).tolist()
        )

    points = [
        models.PointStruct(
            id=str(uuid.uuid4()),
            vector=embeddings[i],
            payload={
                "text":       chunks[i]["text"],
                "source":     source_name,
                "section":    chunks[i]["section"],
                "chunk_type": chunks[i]["chunk_type"],
                "lang":       chunks[i].get("lang", ""),
            },
        )
        for i in range(len(chunks))
    ]

    client.upsert(collection_name=collection_name, points=points)
    return len(chunks)


def index_documents(
    input_dir:       Path,
    collection_name: str,
    model_name:      str,
    chunk_size:      int,
    chunk_overlap:   int,
    reset:           bool,
) -> None:
    print(f"Загрузка модели: {model_name}")
    embedder = SentenceTransformer(model_name)
    vector_size = embedder.get_sentence_embedding_dimension()

    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.QDRANT_API_KEY,
        prefer_grpc=settings.QDRANT_PREFER_GRPC,
    )

    _ensure_collection(client, collection_name, vector_size, reset)

    doc_files = list(_iter_doc_files(input_dir))
    if not doc_files:
        print(f"Не найдено .md/.txt файлов в {input_dir}")
        sys.exit(1)

    print(f"Найдено документов: {len(doc_files)}\n")

    total = 0
    for path in doc_files:
        source_name = str(path.relative_to(input_dir))
        blocks = parse_file(path)
        chunks = list(build_chunks(blocks, chunk_size, chunk_overlap))
        added = _upsert_chunks(
            client, collection_name, chunks, source_name, embedder)
        total += added
        print(f"[{source_name}] чанков добавлено: {added}")

    print(f"\nГотово. Итого чанков в '{collection_name}': {total}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Индексация технической документации (Markdown/.txt) → Qdrant")
    parser.add_argument("--input_dir",     required=True, type=Path)
    parser.add_argument("--collection",    default=settings.QDRANT_COLLECTION)
    parser.add_argument("--model",         default=settings.EMBED_MODEL)
    parser.add_argument("--chunk_size",    type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk_overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--reset",         action="store_true",
                        help="Удалить коллекцию перед индексацией")

    args = parser.parse_args()
    index_documents(
        input_dir=args.input_dir,
        collection_name=args.collection,
        model_name=args.model,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        reset=args.reset,
    )


if __name__ == "__main__":
    main()
