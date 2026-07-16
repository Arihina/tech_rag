"""
Иерархический чанкинг технической документации (Markdown / plain text).

Аналог связки parse_md_blocks + build_chunks из epoz/create_collection_hybrid.py,
но с поправкой на природу технической документации:

  - fenced code-блоки (```lang ... ```) выделяются в отдельный chunk_type="code"
    и НЕ проходят через сентенс-сплиттер общего назначения — резать код по
    границам предложений бессмысленно и ломает синтаксис. У epoz такого типа
    чанков не было (входные данные — юридические .docx/.md без кода).
  - Слишком длинный code-блок режется по границам строк (см. _split_code),
    а не по регэксп-предложениям.
  - HTML- и Markdown-таблицы обрабатываются как в epoz (общий chunk_type="table").
  - breadcrumb по заголовкам (# .. ######) работает так же, как в epoz —
    section собирается из активных уровней заголовков.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Generator

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 80
MAX_CODE_CHUNK = 3000

Block = dict  # {"type": str, "level": int, "text": str, "lang": str, "anchor": str}


_MD_HEADING = re.compile(r'^(#{1,6})\s+(.+)', re.MULTILINE)
_MD_TABLE_ROW = re.compile(r'^\|.+\|')
_MD_TABLE_SEP = re.compile(r'^\|[-| :]+\|')
_MD_FENCE = re.compile(r'^```(\S*)\s*$')

_HTML_TABLE = re.compile(r'<table[\s>].*?</table>', re.IGNORECASE | re.DOTALL)


def _parse_html_table(html: str) -> str:
    def cell_text(cell_html: str) -> str:
        text = re.sub(r'<br\s*/?>', ' ', cell_html, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        return re.sub(r'\s+', ' ', text).strip()

    rows_text = []
    for tr in re.split(r'<tr[\s>]', html, flags=re.IGNORECASE):
        cells = re.findall(
            r'<t[dh][^>]*>(.*?)</t[dh]>',
            tr, flags=re.IGNORECASE | re.DOTALL
        )
        row_parts = [cell_text(c) for c in cells if cell_text(c)]
        if row_parts:
            rows_text.append(' | '.join(row_parts))
    return '\n'.join(rows_text)


def _parse_md_table(lines: list[str]) -> str:
    rows = []
    for line in lines:
        if _MD_TABLE_SEP.match(line.strip()):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(" | ".join(c for c in cells if c))
    return "\n".join(rows)


def parse_markdown_blocks(text: str) -> list[Block]:
    """
    Стратегия (в порядке приоритета):
      - <table>...</table> (HTML)                → block type=table
      - ```lang ... ```  (fenced code)            → block type=code, lang=...
      - '# ...'                                    → block type=heading, level=len('#')
      - строки-таблицы вида '| a | b |'           → block type=table
      - всё остальное                              → block type=text
    """
    html_tables: list[str] = []
    PLACEHOLDER = "\x00TABLE{}\x00"

    def _replace_table(m: re.Match) -> str:
        html_tables.append(m.group(0))
        return PLACEHOLDER.format(len(html_tables) - 1) + "\n"

    text_no_tables = _HTML_TABLE.sub(_replace_table, text)
    lines = text_no_tables.splitlines()
    blocks: list[Block] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        ph_match = re.match(r'\x00TABLE(\d+)\x00', line)
        if ph_match:
            idx = int(ph_match.group(1))
            table_text = _parse_html_table(html_tables[idx])
            if table_text.strip():
                blocks.append({"type": "table", "level": 0,
                               "text": table_text, "lang": "", "anchor": ""})
            i += 1
            continue

        fence = _MD_FENCE.match(line)
        if fence:
            lang = fence.group(1)
            code_lines = []
            i += 1
            while i < len(lines) and not _MD_FENCE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            i += 1
            code_text = "\n".join(code_lines).strip("\n")
            if code_text.strip():
                blocks.append({"type": "code", "level": 0,
                               "text": code_text, "lang": lang, "anchor": ""})
            continue

        m = _MD_HEADING.match(line)
        if m:
            level = len(m.group(1))
            heading = re.sub(r'[*_`]', '', m.group(2)).strip()
            if heading:
                blocks.append({"type": "heading", "level": level,
                               "text": heading, "lang": "", "anchor": ""})
            i += 1
            continue

        if _MD_TABLE_ROW.match(line.strip()):
            table_lines = []
            while i < len(lines) and (
                _MD_TABLE_ROW.match(lines[i].strip()) or
                _MD_TABLE_SEP.match(lines[i].strip())
            ):
                table_lines.append(lines[i])
                i += 1
            table_text = _parse_md_table(table_lines)
            if table_text.strip():
                blocks.append({"type": "table", "level": 0,
                               "text": table_text, "lang": "", "anchor": ""})
            continue

        para_lines = []
        while i < len(lines):
            l = lines[i]
            if (_MD_HEADING.match(l) or _MD_TABLE_ROW.match(l.strip())
                    or _MD_FENCE.match(l)
                    or re.match(r'\x00TABLE\d+\x00', l)):
                break
            stripped = re.sub(r'!\[.*?\]\(.*?\)', '', l.strip())
            stripped = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', stripped)
            if stripped:
                para_lines.append(stripped)
            elif para_lines:
                break
            i += 1

        para_text = " ".join(para_lines).strip()
        if para_text:
            blocks.append({"type": "text", "level": 0,
                           "text": para_text, "lang": "", "anchor": ""})

    return blocks


def parse_plain_text_blocks(text: str) -> list[Block]:
    """Для .txt — просто параграфы по пустым строкам, без структуры."""
    blocks: list[Block] = []
    for para in re.split(r'\n\s*\n', text):
        para = para.strip()
        if para:
            blocks.append({"type": "text", "level": 0,
                           "text": para, "lang": "", "anchor": ""})
    return blocks


def _split_long_text(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks, current = [], ""
    for sent in sentences:
        if len(current) + len(sent) + 1 > size and current:
            chunks.append(current.strip())
            current = (current[-overlap:] + " " + sent) if overlap else sent
        else:
            current = (current + " " + sent).strip()
    if current:
        chunks.append(current.strip())
    return chunks or [text]


def _split_code(text: str, size: int) -> list[str]:
    """Режет длинный код по границам строк, а не предложений."""
    if len(text) <= size:
        return [text]
    lines = text.split("\n")
    chunks, current = [], []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > size and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


def build_chunks(
    blocks:        list[Block],
    chunk_size:    int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> Generator[dict, None, None]:
    breadcrumb: dict[int, str] = {}
    buffer_text = ""

    def flush(btext: str) -> "dict | None":
        btext = btext.strip()
        if not btext:
            return None
        section = " > ".join(v for _, v in sorted(breadcrumb.items()) if v)
        return {"text": btext, "section": section,
                "chunk_type": "paragraph", "lang": ""}

    for block in blocks:
        btype = block["type"]
        level = block["level"]
        text = block["text"]

        if btype == "heading":
            if buffer_text:
                result = flush(buffer_text)
                if result:
                    yield result
                buffer_text = ""

            breadcrumb = {k: v for k, v in breadcrumb.items() if k < level}
            breadcrumb[level] = text
            section = " > ".join(v for _, v in sorted(breadcrumb.items()) if v)
            yield {"text": text, "section": section,
                   "chunk_type": "heading", "lang": ""}

        elif btype == "table":
            if buffer_text:
                result = flush(buffer_text)
                if result:
                    yield result
                buffer_text = ""

            section = " > ".join(v for _, v in sorted(breadcrumb.items()) if v)
            yield {"text": text, "section": section,
                   "chunk_type": "table", "lang": ""}

        elif btype == "code":
            if buffer_text:
                result = flush(buffer_text)
                if result:
                    yield result
                buffer_text = ""

            section = " > ".join(v for _, v in sorted(breadcrumb.items()) if v)
            lang = block.get("lang", "")
            for part in _split_code(text, MAX_CODE_CHUNK):
                yield {"text": part, "section": section,
                       "chunk_type": "code", "lang": lang}

        else:  # text / paragraph
            if len(buffer_text) + len(text) + 1 > chunk_size and buffer_text:
                result = flush(buffer_text)
                if result:
                    for part in _split_long_text(result["text"], chunk_size, chunk_overlap):
                        yield {**result, "text": part}
                buffer_text = (
                    buffer_text[-chunk_overlap:] + " " + text) if chunk_overlap else text
            else:
                buffer_text = (buffer_text + "\n" + text).strip()

    if buffer_text:
        result = flush(buffer_text)
        if result:
            for part in _split_long_text(result["text"], chunk_size, chunk_overlap):
                yield {**result, "text": part}


def parse_file(path: Path) -> list[Block]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".md", ".markdown"):
        return parse_markdown_blocks(text)
    return parse_plain_text_blocks(text)
