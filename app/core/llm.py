from __future__ import annotations
import re
from typing import Generator
import ollama

from app.core.retrieval import retrieve, DocResult
from app.core.config import settings


OLLAMA_HOST = settings.OLLAMA_HOST
OLLAMA_MODEL = settings.OLLAMA_MODEL
RAG_MIN_SCORE = 0.013

REFUSAL_TEXT = (
    "В загруженной технической документации ответ на этот вопрос не найден. "
    "Уточните формулировку или проверьте, что нужный документ загружен."
)

_CITE_RE = re.compile(r"\[(\d{1,2})\]")


_client = ollama.Client(host=OLLAMA_HOST)


_SMALL_TALK_PATTERNS = (
    "привет", "здравств", "как тебя", "кто ты",
    "кто я", "как меня зовут", "меня зовут",
    "hello", "hi",
)


def is_small_talk(text: str) -> bool:
    """Только короткие приветственные реплики, не технические вопросы."""
    stripped = text.strip()
    if len(stripped) > 40:
        return False
    lower = stripped.lower()
    return any(p in lower for p in _SMALL_TALK_PATTERNS)


def _format_history(
    history: list[tuple[str, str]],
    max_turns: int = 10,
    max_chars: int | None = None,
) -> str:
    recent = history[-(max_turns * 2):]
    max_chars = max_chars if max_chars is not None else settings.HISTORY_MAX_CHARS
    lines = []
    total = 0
    for role, text in reversed(recent):
        label = "Пользователь" if role == "user" else "Ассистент"
        line = f"{label}: {text}"
        if total + len(line) > max_chars and lines:
            break
        lines.append(line)
        total += len(line)
    lines.reverse()
    return "\n".join(lines)


def _instructions_block(instructions: str | None) -> str:
    if not instructions or not instructions.strip():
        return ""
    return f"\nДополнительные инструкции пользователя:\n{instructions.strip()}\n"


def _general_prompt(
    history: list[tuple[str, str]],
    question: str,
    instructions: str | None = None,
) -> str:
    return f"""Ты русскоязычный AI ассистент.
Отвечай ТОЛЬКО на русском языке.
Это общий вопрос, не связанный с документами.
Отвечай свободно, как обычный ассистент.
Источники указывать НЕ НУЖНО.
{_instructions_block(instructions)}
Предыдущий диалог:
{_format_history(history)}

Вопрос пользователя:
{question}

Ответ:""".strip()


def _doc_location(doc: DocResult) -> str:
    parts = []
    source = doc.get("source")
    if source:
        parts.append(str(source))
    section = doc.get("section")
    if section:
        parts.append(str(section))
    return " · ".join(parts)


def _build_context(docs: list[DocResult], max_chars: int) -> str:
    parts: list[str] = []
    total = 0
    for i, doc in enumerate(docs, 1):
        location = _doc_location(doc)
        header = f"[{i}]" + (f" {location}" if location else "")
        block = f"{header}\n{doc['text']}"
        if total + len(block) > max_chars and parts:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def _rag_prompt(
    docs: list[DocResult],
    history: list[tuple[str, str]],
    question: str,
    instructions: str | None = None,
) -> str:
    context = _build_context(docs, settings.MAX_CONTEXT_CHARS)
    return f"""Ты русскоязычный AI ассистент.
Отвечай ТОЛЬКО на русском языке.
Используй ТОЛЬКО информацию из контекста ниже. Не придумывай факты, которых нет в контексте.
Ссылайся на использованные фрагменты в квадратных скобках: [1], [2] и т.д. по нумерации контекста.
Если в контексте нет ответа — прямо скажи об этом, не пытайся угадать.
{_instructions_block(instructions)}
Контекст:
{context}

Предыдущий диалог:
{_format_history(history)}

Вопрос пользователя:
{question}

Ответ:""".strip()


def _sources_from_citations(answer: str, docs: list[DocResult]) -> list[str]:
    """Извлекает использованные источники из цитат [n] в ответе."""
    used: set[str] = set()
    for m in _CITE_RE.finditer(answer):
        n = int(m.group(1))
        if 1 <= n <= len(docs):
            used.add(docs[n - 1].get("source", "unknown"))
    return sorted(used)


def find_used_sources(answer: str, docs: list[DocResult]) -> list[str]:
    """Fallback, если модель не поставила цитаты [n]."""
    lower = answer.lower()
    used: set[str] = set()
    for doc in docs:
        overlap = sum(1 for w in doc["text"].lower().split() if w in lower)
        if overlap > 5:
            used.add(doc["source"])
    return sorted(used)


def _resolved_sources(answer: str, docs: list[DocResult]) -> list[str] | None:
    """Цитаты [n] — основной способ, эвристика — только запасной."""
    cited = _sources_from_citations(answer, docs)
    if cited:
        return cited
    overlap = find_used_sources(answer, docs)
    return overlap or None


def _retrieval_query(question: str) -> str:
    return question.replace("?", "").strip()


def _model_options(options: dict | None) -> dict | None:
    opts = dict(options or {})
    opts.setdefault("num_ctx", settings.OLLAMA_NUM_CTX)
    if settings.OLLAMA_NUM_PREDICT is not None:
        opts.setdefault("num_predict", settings.OLLAMA_NUM_PREDICT)
    return opts or None


def _empty_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def stream_answer(
    question: str,
    history: list[tuple[str, str]],
    instructions: str | None = None,
    options: dict | None = None,
) -> Generator[tuple[str, list[str] | None, list[DocResult] | None, dict | None], None, None]:
    refusal = False
    docs: list[DocResult] | None = None

    if is_small_talk(question):
        prompt = _general_prompt(history, question, instructions)
    else:
        docs = retrieve(_retrieval_query(question))
        if not docs or docs[0]["score"] < RAG_MIN_SCORE:
            if settings.ALLOW_GENERAL_ANSWER:
                prompt = _general_prompt(history, question, instructions)
                docs = None
            else:
                refusal = True
                prompt = ""
        else:
            prompt = _rag_prompt(docs, history, question, instructions)

    if refusal:
        yield "", None, docs, None
        yield REFUSAL_TEXT, None, None, None
        yield "", None, None, _empty_usage()
        return

    yield "", None, docs, None

    stream = _client.chat(
        model=OLLAMA_MODEL,
        messages=[{"role": "user", "content": prompt}],
        stream=True,
        options=_model_options(options),
    )
    full_answer = ""
    prompt_tokens = 0
    completion_tokens = 0
    for chunk in stream:
        token: str = chunk["message"]["content"]
        full_answer += token
        if token:
            yield token, None, None, None
        if chunk.get("done"):
            prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
            completion_tokens = chunk.get("eval_count", 0) or 0

    used_sources: list[str] | None = None
    if docs:
        used_sources = _resolved_sources(full_answer, docs)

    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    yield "", used_sources, None, usage
