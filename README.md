# TechDocs RAG Agent

RAG-агент для технической документации.


- **Источник документов** — `.md`/`.txt`
  файлы (структура типичного репозитория с документацией: `README.md`,
  `docs/`), с отдельным типом чанка `code` для фрагментов кода — они не
  режутся сентенс-сплиттером, чтобы не ломать синтаксис.


## Чанкинг технической документации

`chunking.py` — иерархический чанкинг с отслеживанием breadcrumb
по заголовкам плюс:

- fenced code-блоки (` ```lang ... ``` `) выделяются в `chunk_type="code"`
  и не проходят через сентенс-сплиттер — режутся только по границам строк
  и только если превышают `MAX_CODE_CHUNK` (3000 символов);
- HTML- и Markdown-таблицы — `chunk_type="table"`;
- обычный текст — `chunk_type="paragraph"`, режется по предложениям с
  перекрытием.

## Запуск

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

sudo docker compose up -d   # поднимет Postgres (5435) и Qdrant (6333/6334)

alembic upgrade head
```

> Схему ведёт только Alembic — приложение не создаёт таблицы при старте.
> `alembic upgrade head` обязателен перед первым запуском.

Индексация документации:

```bash
python3 -m index_qdrant \
    --input_dir ./docs \
    --collection techdocs_hybrid \
    --reset
```

Пример заполнения `.env`
```
# --- Postgres ---
DB_HOST=localhost
DB_PORT=5435
DB_USER=postgres
DB_PASSWORD=postgres
DB_NAME=techdocs_rag_db

# --- Qdrant ---
QDRANT_HOST=localhost
QDRANT_PORT=6333
QDRANT_COLLECTION=techdocs_hybrid

# --- Модели ---
EMBED_MODEL=intfloat/multilingual-e5-small
OLLAMA_MODEL=gemma2:9b
OLLAMA_HOST=http://localhost:11434
```

Запуск сервиса:

```bash
python3 main.py
# или
uvicorn main:app --host 127.0.0.1 --port 8004 --reload
```

Веб-консоль Qdrant для отладки коллекции: `http://localhost:6333/dashboard`.

## Аутентификация

Сервис не управляет пользователями, это задача платформы
(мастер-агент + Keycloak). Агент получает UUID пользователя в заголовке
`X-User-Id` и использует его как скоуп для своих данных. Заголовок
обязателен во **всех** запросах.

| Ситуация | Код |
|----------|-----|
| Заголовок `X-User-Id` отсутствует | `401` |
| `X-User-Id` не является валидным UUID | `401` |
| Обращение к чужому чату/сообщению | `404` |

## Контракт (соответствие `master_node`)

Все обязательные пункты канонического контракта реализованы:

- `POST /sessions` → объект с полем `id`.
- `GET /sessions`, `GET /sessions/{id}/messages`, `PATCH /sessions/{id}`,
  `DELETE /sessions/{id}`.
- `POST /sessions/{id}/chat` → SSE в формате:

  ```
  data: {"chunks": [{"text": "...", "source": "docs/api.md", "score": 0.87}]}
  data: {"token": "Для"}
  data: {"token": " установки"}
  ...
  data: {"message_id": "1e6b7ee7-..."}
  data: [DONE]
  ```

- `POST/GET/DELETE /messages/{id}/feedback`.

## База данных

PostgreSQL 16:
`chat_sessions` / `chat_messages` / `message_feedback`, все идентификаторы
сущностей — UUID, генерируются на стороне приложения. Порядок сообщений —
по `created_at`, не по `id`.

## Модели

Настраиваются через `.env`:

```env
EMBED_MODEL=BAAI/bge-m3
OLLAMA_MODEL=qwen3.6:35b
```

## Примеры curl

```bash
U=11111111-1111-1111-1111-111111111111

curl -X POST http://127.0.0.1:8004/sessions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"title": "Тестовый чат"}'

SID=<id-из-ответа>

curl -N -X POST http://127.0.0.1:8004/sessions/$SID/chat \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"message": "Как установить пакет?"}' \
  --no-buffer
```
