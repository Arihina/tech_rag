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

sudo docker compose up -d   # поднимет Postgres (5435) и Qdrant 6334

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
QDRANT_PORT=6334
QDRANT_COLLECTION=techdocs_hybrid

# --- Модели ---
EMBED_MODEL=intfloat/multilingual-e5-small
OLLAMA_MODEL=gemma2:9b
OLLAMA_HOST=http://localhost:11434   # читается приложением; в docker-развёртывании — имя сервиса, а не localhost
```

Запуск сервиса:

```bash
python3 main.py
# или
uvicorn main:app --host 127.0.0.1 --port 8004 --reload
```

Веб-консоль Qdrant для отладки коллекции: `http://localhost:6334/dashboard`.

## Модели

Настраиваются через `.env`:

```env
EMBED_MODEL=BAAI/bge-m3
OLLAMA_MODEL=qwen3.6:35b
```

## База данных
Миграции управляются через Alembic. Применить:
```bash
alembic upgrade head
```

```
conversations                    платформенная сущность, только для UI/истории
├── id          UUID          PK, генерируется приложением (uuid4)
├── user_id     UUID          NOT NULL, индексирован — владелец чата
├── title       VARCHAR(255)  nullable (подставляется из первого вопроса, если не указан)
├── created_at  TIMESTAMPTZ
└── updated_at  TIMESTAMPTZ

chat_messages
├── id                 UUID          PK, генерируется приложением (uuid4);
│                                    у ассистентских сообщений совпадает с
│                                    частью после "chatcmpl-" в id completion'а
├── user_id            UUID          NOT NULL, индексирован — владелец записи
├── conversation_id    UUID          nullable, FK → conversations.id (ON DELETE CASCADE).
│                                    НЕ участвует в сборке контекста для генерации —
│                                    только привязка к чату в UI-списке
├── role                VARCHAR(16)   "user" | "assistant"
├── content             TEXT
├── sources             JSONB         источники, реально использованные в ответе
├── retrieved_chunks    JSONB         весь пул извлечённых фрагментов до фильтрации (для /sources)
├── model               VARCHAR(64)   значение "model" из запроса
└── created_at          TIMESTAMPTZ

message_feedback
├── id          SERIAL        PK (идентификатор самой записи фидбэка, не используется в API)
├── message_id  UUID          FK → chat_messages.id (уникальный — одна оценка на сообщение)
├── vote        INTEGER       1 = лайк / -1 = дизлайк / NULL = без оценки
├── comment     TEXT          nullable
├── created_at  TIMESTAMPTZ
└── updated_at  TIMESTAMPTZ
```

Все идентификаторы — UUID, генерируются на стороне приложения (`default=uuid4`). `chat_messages` скоупится напрямую по `user_id`; принадлежность чату (`conversation_id`) опциональна и не связана с тем, кто владеет записью. Удаление `conversation` каскадно удаляет её сообщения; удаление сообщения каскадно удаляет его фидбэк.


## Аутентификация

Сервис не управляет пользователями — это задача платформы (мастер-агент + Keycloak). RAG-ассистент получает UUID пользователя в заголовке `X-User-Id` и использует его как скоуп для своих данных. Заголовок обязателен **во всех** запросах:

```
X-User-Id: 11111111-1111-1111-1111-111111111111
```

JWT валидирует мастер-агент; RAG доверяет внутреннему трафику (сервис должен быть закрыт снаружи в обход мастера). При переходе на валидацию JWT по JWKS Keycloak меняется только `get_user_id`, эндпоинты не затрагиваются.

| Ситуация | Код |
|----------|-----|
| Заголовок `X-User-Id` отсутствует | `401` |
| `X-User-Id` не является валидным UUID | `401` |
| Обращение к чужому completion'у, чату (`conversation`) или фидбэку | `404` |

Возврат `404` (а не `403`) для чужих объектов сознателен: сервис не подтверждает их существование.

## API — общая идея

API состоит из трёх частей:

- **`/v1/chat/completions`** — форма OpenAI Chat Completions. **Полностью stateless**: сервис не хранит и не переиспользует историю диалога — клиент присылает её целиком в `messages[]` при каждом запросе. Формат запроса/ответа соответствует `chat.completion` / `chat.completion.chunk`.
- **`/v1/responses`** — форма OpenAI Responses API. Та же генерация, другой формат: объект `response` с массивом `output` вместо `choices`, стрим — типизированными SSE-событиями вместо чанков. В отличие от Chat Completions, умеет работать с историей на стороне сервера: при переданном `conversation` (или `previous_response_id`) историю собирает сам агент из БД.
- **`/v1/platform/conversations`**, **`.../feedback`**, **`.../sources`** — платформенные (не входящие в OpenAI-стандарт) расширения для UI: список чатов, история сообщений, оценка ответа, извлечённые фрагменты. Связь с формами генерации — поле `conversation_id` в теле запроса и `id` ответа как ключ для фидбэка и источников.

Обе формы совместимы с официальным SDK: `OpenAI(base_url=..., default_headers={"X-User-Id": ...})`, дальше `client.chat.completions.create(...)` или `client.responses.create(...)`. Совместимость проверяется тестом `test_openai_conformance.py`, который валидирует реальные ответы обеих форм против pydantic-моделей `openai-python`.


## API

Все эндпоинты требуют заголовок `X-User-Id: <uuid>`. Ресурсы скоупятся по этому идентификатору; обращение к чужому ресурсу возвращает `404`.

### `POST /v1/chat/completions`

Генерация ответа. Клиент присылает **всю** историю диалога в `messages[]` — сервис её не хранит и не переиспользует между запросами.

Тело запроса:
```json
{
  "model": "tech_rag",
  "messages": [
    {"role": "user", "content": "что такое РАГ"},
    {"role": "assistant", "content": "РАГ — это..."},
    {"role": "user", "content": "а какие сроки"}
  ],
  "stream": true,
  "conversation_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```
| Поле | Тип | Обязательно | Описание |
|---|---|---|---|
| `model` | string | нет (по умолчанию `"tech_rag"`) | не влияет на поведение, только эхом в ответе |
| `messages` | array | да | последнее сообщение — `role: "user"`, это и есть текущий вопрос. `content` принимается и строкой, и массивом частей (`{"type": "text", ...}`) |
| `stream` | bool | нет (по умолчанию `false`) | стримить ответ через SSE |
| `stream_options.include_usage` | bool | нет | добавить в конец стрима чанк с `usage` и пустым `choices` |
| `temperature`, `top_p` | number | нет | передаются модели как есть |
| `max_tokens` / `max_completion_tokens` | int | нет | ограничение длины ответа (`num_predict` для Ollama) |
| `n` | int | нет | поддерживается только `1`; другое значение → `400`, а не тихий возврат одного варианта |
| `conversation_id` | UUID string | нет | платформенное расширение — привязать сообщение к чату из `/v1/platform/conversations`. Чужой/несуществующий `conversation_id` → `404` |

Сообщения с `role: "system"` / `"developer"` попадают в промпт как дополнительные инструкции пользователя: они уточняют поведение, но не отменяют базовые правила сервиса (язык ответа, работа с источниками).

**Не поддерживаются** (молча игнорируются): `tools`, `tool_choice`, `response_format`, `seed`, `logprobs`, `logit_bias`, `presence_penalty`, `frequency_penalty`, `stop`, `user`, `service_tier`.

**Нестрим-ответ** (`chat.completion`):
```json
{
  "id": "chatcmpl-1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
  "object": "chat.completion",
  "created": 1735900000,
  "model": "tech_rag",
  "conversation_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "system_fingerprint": null,
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "РАГ (Retrieval-Augmented Generation) — это...\n\nПроанализированные источники:\n- doc1.pdf",
      "refusal": null,
      "annotations": []
    },
    "logprobs": null,
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 412, "completion_tokens": 87, "total_tokens": 499}
}
```

**Стрим-ответ** (`stream: true`) — SSE, `chat.completion.chunk`, один и тот же `id` во всех чанках:
```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1735900000,"model":"tech_rag","conversation_id":"3fa85f64-...","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1735900000,"model":"tech_rag","conversation_id":"3fa85f64-...","choices":[{"index":0,"delta":{"content":"РАГ"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1735900000,"model":"tech_rag","conversation_id":"3fa85f64-...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```
`conversation_id` в ответе — `null`, если не был передан в запросе. `id` (`chatcmpl-<uuid>`) — ключ для фидбэка, источников, повторного чтения и удаления. Блок «Проанализированные источники: ...» добавляется в `content` в конце ответа, если ассистент использовал извлечённые фрагменты (как обычный текст, не отдельным полем) — одинаково в стриме и без него, и одинаково в обеих формах.

Поля `system_fingerprint`, `refusal`, `annotations`, `logprobs` сервис не наполняет, но возвращает: они входят в объект по спецификации, и часть клиентов на них закладывается.

---

### `GET /v1/chat/completions/{completion_id}`

Повторное чтение сохранённого ответа. Возвращает тот же объект `chat.completion`, что и генерация. `{completion_id}` — `chatcmpl-<uuid>` целиком или голый UUID. Чужой или несуществующий id → `404`, синтаксически некорректный → `400`.

---

### `DELETE /v1/chat/completions/{completion_id}`

Удаляет сообщение ассистента вместе с его фидбэком (каскад по FK):
```json
{"id": "chatcmpl-1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2", "object": "chat.completion.deleted", "deleted": true}
```
Реплика пользователя, вызвавшая ответ, **остаётся** в чате: id адресует именно объект completion, а связи «ответ → вопрос» в схеме нет — сообщения связаны только чатом и порядком `created_at`. Чтобы убрать оба сообщения, удаляйте чат целиком через `/v1/platform/conversations/{id}`.

---

### `POST /v1/responses`

Та же генерация в форме Responses API. Два режима работы в зависимости от того, передан ли чат:

- **без `conversation`** — stateless, как Chat Completions: вся история приходит в `input[]`;
- **с `conversation`** — историю собирает агент из БД (последние `HISTORY_LIMIT` сообщений чата), а `input` должен содержать **только новый ход**.

Тело запроса:
```json
{
  "model": "tech_rag",
  "input": [
    {"role": "user", "content": [{"type": "input_text", "text": "что такое РАГ"}]}
  ],
  "instructions": "Отвечай кратко",
  "stream": true,
  "conversation": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```
| Поле | Тип | Обязательно | Описание |
|---|---|---|---|
| `model` | string | нет (по умолчанию `"tech_rag"`) | эхом в ответе |
| `input` | string \| array | да | строка либо список items; последний — `role: "user"` |
| `instructions` | string | нет | дополнительные инструкции; кладутся в промпт после базовых правил сервиса |
| `stream` | bool | нет | стримить типизированными SSE-событиями |
| `store` | bool | нет (по умолчанию `true`) | `false` — не сохранять ответ в БД; тогда `GET /v1/responses/{id}` по нему вернёт `404`, а фидбэк и источники будут недоступны |
| `temperature`, `top_p`, `max_output_tokens` | number/int | нет | передаются модели |
| `conversation` | string \| `{"id": ...}` | нет | чат из `/v1/platform/conversations`. `conversation_id` принимается как алиас — для фронта платформы; официальный SDK такого именованного аргумента не знает и отправил бы его только через `extra_body` |
| `previous_response_id` | string | нет | стандартный способ продолжить цепочку: агент находит предыдущий ответ и берёт чат, которому тот принадлежал. Неизвестный или чужой id → `404` |
| `metadata` | object | нет | не используется, возвращается эхом |

**Не поддерживаются**: `tools`, `text.format`, `reasoning`, `include`, `background`, `truncation`.

Если при переданном `conversation` в `input` всё-таки пришла история — `400`. Это сознательное отступление от спецификации OpenAI (там `conversation` вместе с многоходовым `input` допустим, история просто дополняется): предпочитаем явную ошибку риску удвоить контекст.

**Нестрим-ответ** (`response`):
```json
{
  "id": "resp_1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
  "object": "response",
  "created_at": 1735900000,
  "status": "completed",
  "model": "tech_rag",
  "output": [{
    "id": "msg_1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
    "type": "message",
    "status": "completed",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "РАГ — это...", "annotations": [], "logprobs": []}]
  }],
  "parallel_tool_calls": false,
  "tool_choice": "auto",
  "tools": [],
  "error": null,
  "incomplete_details": null,
  "instructions": null,
  "metadata": {},
  "temperature": null,
  "top_p": null,
  "max_output_tokens": null,
  "previous_response_id": null,
  "store": true,
  "truncation": "disabled",
  "text": {"format": {"type": "text"}},
  "conversation_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "usage": {
    "input_tokens": 412,
    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    "output_tokens": 87,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 499
  }
}
```
Поля `parallel_tool_calls`, `tool_choice`, `tools` и подобъекты `*_tokens_details` обязательны в объекте `response` — без них официальный SDK не разбирает ответ, даже если инструменты не поддерживаются. `conversation_id` — расширение платформы, остальное — из спецификации.

**Стрим-ответ** (`stream: true`) — SSE с типом в поле `event` и монотонным `sequence_number`:
```
event: response.created          # объект response со status: "in_progress"
event: response.in_progress
event: response.output_item.added
event: response.content_part.added
event: response.output_text.delta   # по одному на токен
...
event: response.output_text.done
event: response.content_part.done
event: response.output_item.done
event: response.completed        # объект response со status: "completed" и usage
```
Терминатора `[DONE]` здесь нет — в отличие от Chat Completions, поток закрывается событием `response.completed`. При ошибке во время генерации приходит событие `error` со следующим по порядку `sequence_number`.

---

### `GET /v1/responses/{response_id}` и `DELETE /v1/responses/{response_id}`

Чтение и удаление — так же, как для Chat Completions выше, но объект `response`, а `deleted`-ответ — `{"id": ..., "object": "response.deleted", "deleted": true}`. Идентификаторы взаимозаменяемы: обе формы адресуют одно и то же сообщение в БД, поэтому ответ, созданный через `/v1/responses`, читается и через `/v1/chat/completions/{id}` (и наоборот) — префикс `resp_` / `chatcmpl-` при разборе отбрасывается.

---

### `POST/GET/DELETE /v1/chat/completions/{completion_id}/feedback`

Оценка ответа ассистента. `{completion_id}` — значение `id` из ответа (`chatcmpl-<uuid>` целиком или голый UUID — оба варианта принимаются).

Тело `POST`-запроса (все поля опциональны, повторный вызов обновляет существующую оценку):
```json
{ "vote": 1, "comment": "Очень подробно" }
```
| Поле | Тип | Значения |
|---|---|---|
| `vote` | integer | `1` — лайк, `-1` — дизлайк, `null`/отсутствует — без оценки |
| `comment` | string | любой текст, опционально |

Ответ (`POST`/`GET`):
```json
{
  "message_id": "1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
  "vote": 1,
  "comment": "Очень подробно",
  "created_at": "2026-08-03T12:01:00",
  "updated_at": "2026-08-03T12:01:00"
}
```
`DELETE` сбрасывает оценку (`vote=null`, `comment=null`) и возвращает `204`.

---

### `GET /v1/chat/completions/{completion_id}/sources`

Источники, использованные в конкретном ответе — замена ушедшему из стрима событию `chunks`.
```json
{
  "id": "chatcmpl-1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
  "retrieved": [{"text": "...", "source": "doc1.pdf", "score": 0.87}],
  "used_sources": ["doc1.pdf"]
}
```
`retrieved` — весь пул извлечённых фрагментов до фильтрации; `used_sources` — то, что действительно попало в ответ (та же эвристика, что и раньше — пересечение слов ответа с текстом фрагмента).

---

### Чаты — `/v1/platform/conversations`

Платформенное расширение для UI (список чатов, история, переименование, удаление). **Не входит в OpenAI-стандарт** и не участвует в генерации — см. «API — общая идея» выше.

#### `POST /v1/platform/conversations`
Создать новый чат.

Тело запроса (опционально): `{ "title": "Название чата" }`

Ответ:
```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "title": "Название чата",
  "created_at": "2026-08-03T12:00:00",
  "updated_at": "2026-08-03T12:00:00"
}
```

#### `GET /v1/platform/conversations`
Список чатов текущего пользователя, отсортированных по дате последнего сообщения (новые первые). Формат элемента — как у `POST`.

#### `GET /v1/platform/conversations/{id}/messages`
История сообщений чата вместе с фидбэком — для восстановления `messages[]` на фронте при открытии чата.
```json
[
  {
    "id": "9c858901-8a57-4791-81fe-4c455b099bc9",
    "role": "user",
    "content": "Что такое РАГ",
    "sources": [],
    "created_at": "2026-08-03T12:00:00",
    "feedback": null
  },
  {
    "id": "1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2",
    "role": "assistant",
    "content": "РАГ (Retrieval-Augmented Generation) — это...",
    "sources": ["doc1.pdf"],
    "created_at": "2026-08-03T12:00:05",
    "feedback": {"vote": 1, "comment": "Хороший ответ"}
  }
]
```
`id` ассистентского сообщения — тот же UUID, что использовать для `/v1/chat/completions/{id}/feedback` и `/sources`.

#### `PATCH /v1/platform/conversations/{id}`
Переименовать чат. Тело: `{ "title": "Новое название" }`.

#### `DELETE /v1/platform/conversations/{id}`
Удалить чат со всеми сообщениями и их фидбэком (каскадно). Ответ: `204 No Content`.

---

## Ошибки

Единый формат вместо FastAPI-дефолта `{"detail": ...}`:
```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": null}}
```
`type` — грубая классификация по HTTP-статусу: `400/413/415/422` → `invalid_request_error`, `401` → `authentication_error`, `404` → `not_found_error`, остальное → `server_error`.

Невалидное тело запроса — **`400`**, а не `422`: OpenAI отвечает на такие запросы именно `400`, а SDK мапит `422` в `UnprocessableEntityError`, мимо клиентского `except BadRequestError`. Поле `param` заполняется путём до проблемного поля (`messages.0.role`), а не остаётся `null`.

---

## Примеры curl
 
```bash
U=11111111-1111-1111-1111-111111111111
 
# --- генерация, без привязки к чату ---
curl -k -X POST http://localhost:8004/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"model": "tech_rag", "messages": [{"role": "user", "content": "что такое Меры ограничительного характера"}]}'
# -> {"id": "chatcmpl-...", "object": "chat.completion", "choices": [...], "usage": {...}}
 
# то же самое, стримом
curl -k -N -X POST http://localhost:8004/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"model": "tech_rag", "stream": true, "messages": [{"role": "user", "content": "что такое РАГ"}]}'
 
# продолжение диалога — история целиком в теле
curl -k -X POST http://localhost:8004/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"model": "tech_rag", "messages": [
        {"role": "user", "content": "что такое РАГ"},
        {"role": "assistant", "content": "РАГ (Retrieval-Augmented Generation) — это..."},
        {"role": "user", "content": "а какие сроки"}
      ]}'
 
ID=chatcmpl-1e6b7ee7-d5bb-4f0a-8f9e-a06f19a8f3c2
 
# получить ответ повторно по id (например, если клиент потерял тело исходного ответа)
curl -k http://localhost:8004/v1/chat/completions/$ID -H "X-User-Id: $U"
 
# источники
curl -k http://localhost:8004/v1/chat/completions/$ID/sources -H "X-User-Id: $U"
 
# --- фидбэк ---
 
# поставить оценку
curl -k -X POST http://localhost:8004/v1/chat/completions/$ID/feedback \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"vote": 1, "comment": "Хороший ответ"}'
 
# посмотреть текущую оценку
curl -k http://localhost:8004/v1/chat/completions/$ID/feedback -H "X-User-Id: $U"
 
# сбросить оценку
curl -k -X DELETE http://localhost:8004/v1/chat/completions/$ID/feedback -H "X-User-Id: $U"
 
# --- чаты (платформенный CRUD) ---
 
# создать чат
curl -k -X POST http://localhost:8004/v1/platform/conversations \
  -H "X-User-Id: $U" -H "Content-Type: application/json" -d '{"title": "Тестовый чат"}'
# -> {"id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", ...}
 
CID=3fa85f64-5717-4562-b3fc-2c963f66afa6
 
# сообщение внутри чата — conversation_id привязывает запись к нему
curl -k -X POST http://localhost:8004/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d "{\"model\": \"tech_rag\", \"conversation_id\": \"$CID\", \"messages\": [{\"role\": \"user\", \"content\": \"что такое РАГ\"}]}"
 
# список чатов
curl -k http://localhost:8004/v1/platform/conversations -H "X-User-Id: $U"
 
# история сообщений чата
curl -k http://localhost:8004/v1/platform/conversations/$CID/messages -H "X-User-Id: $U"
 
# переименовать / удалить
curl -k -X PATCH http://localhost:8004/v1/platform/conversations/$CID \
  -H "X-User-Id: $U" -H "Content-Type: application/json" -d '{"title": "Новое название"}'
curl -k -X DELETE http://localhost:8004/v1/platform/conversations/$CID -H "X-User-Id: $U"
 
# --- ошибки ---
 
# без X-User-Id -> 401 в едином формате
curl -k -X POST http://localhost:8004/v1/chat/completions \
  -H "Content-Type: application/json" -d '{"messages": [{"role": "user", "content": "привет"}]}'
# -> {"error": {"message": "...", "type": "authentication_error", "param": null, "code": null}}
 
# чужой/несуществующий completion_id -> 404
curl -k http://localhost:8004/v1/chat/completions/chatcmpl-00000000-0000-0000-0000-000000000000/sources \
  -H "X-User-Id: $U"
# -> {"error": {"message": "Сообщение не найдено", "type": "not_found_error", "param": null, "code": null}}
 
# --- форма Responses ---
curl -k -X POST http://localhost:8004/v1/responses \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d '{"model": "tech_rag", "input": "что такое РАГ"}'

# продолжение цепочки стандартным способом
RID=$(curl -sk -X POST http://localhost:8004/v1/responses \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d "{\"conversation\": \"$CID\", \"input\": \"что такое РАГ\"}" | jq -r .id)
curl -k -X POST http://localhost:8004/v1/responses \
  -H "X-User-Id: $U" -H "Content-Type: application/json" \
  -d "{\"previous_response_id\": \"$RID\", \"input\": \"а какие сроки\"}"

# удаление ответа (фидбэк уходит каскадом)
curl -k -X DELETE http://localhost:8004/v1/responses/$RID -H "X-User-Id: $U"
# -> {"id": "resp_...", "object": "response.deleted", "deleted": true}

# пустой messages -> 400
curl -k -X POST http://localhost:8004/v1/chat/completions \
  -H "X-User-Id: $U" -H "Content-Type: application/json" -d '{"messages": []}'
# -> {"error": {"message": "messages обязателен и не должен быть пустым", "type": "invalid_request_error", "param": null, "code": null}}
```