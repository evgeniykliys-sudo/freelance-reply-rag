"""Строит векторный индекс из FREELANCE.md — не общий параграфный чанкинг
(как в rag-faq-bot), а разбор под структуру конкретно этого документа:
строки таблицы услуг и портфолио — по одной как чанк, бриф-шаблоны и
шаблоны откликов — каждый целиком.

Источник один (FREELANCE.md), копия контента никуда не дублируется — при
изменении услуг/цен там нужно просто перезапустить ingest.py.
"""
import re
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

FREELANCE_MD = Path(r"C:\Projects\_docs\FREELANCE.md")
CHROMA_DIR = Path(__file__).parent / "chroma_db"
COLLECTION_NAME = "freelance"
# Многоязычная модель — документ и входящие заказы на русском, all-MiniLM-L6-v2
# (как в rag-faq-bot) для русского подходит хуже, т.к. обучена в основном на английском.
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

# Разделы документа, которые используются как материал для черновика ответа клиенту.
# "Стратегия" и "Платформы" сознательно не индексируются — это внутренние заметки
# (демпинг, какие каналы живые и т.п.), не то, что должно попасть в текст клиенту.
INCLUDED_SECTIONS = {
    "Навыки для профиля",
    "Услуги (Кворки)",
    "Шаблоны откликов",
    "Публичные проекты (для откликов)",
}
BRIEF_PREFIX = "Бриф-шаблон:"


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Делит документ по заголовкам ## — возвращает [(заголовок, тело)]."""
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    # parts[0] — всё до первого ##, дальше идут пары (заголовок, тело)
    sections = []
    for i in range(1, len(parts), 2):
        sections.append((parts[i].strip(), parts[i + 1].strip()))
    return sections


def _parse_table(body: str) -> list[dict]:
    """Простой парсер markdown-таблицы: первая строка — заголовки колонок."""
    lines = [l for l in body.splitlines() if l.strip().startswith("|")]
    if len(lines) < 2:
        return []
    headers = [h.strip() for h in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[2:]:  # пропускаем заголовок и строку-разделитель ---
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def chunks_from_services(body: str) -> list[tuple[str, dict]]:
    out = []
    for row in _parse_table(body):
        text = (
            f"Услуга: {row['Услуга']}\n"
            f"Цена: {row['Цена']}\n"
            f"Входит: {row['Входит']}\n"
            f"НЕ входит: {row['НЕ входит']}"
        )
        out.append((text, {"type": "service", "name": row["Услуга"]}))
    return out


def chunks_from_portfolio(body: str) -> list[tuple[str, dict]]:
    out = []
    for row in _parse_table(body):
        text = (
            f"Проект портфолио: {row['Проект']}\n"
            f"Стек: {row['Стек']}\n"
            f"Ссылка: {row['Ссылка']}"
        )
        out.append((text, {"type": "portfolio", "name": row["Проект"]}))
    return out


def chunks_from_templates(body: str) -> list[tuple[str, dict]]:
    # Формат: **Заголовок:**\n> текст шаблона
    out = []
    for m in re.finditer(r"\*\*(.+?):\*\*\s*\n>\s*(.+)", body):
        label, quote = m.group(1).strip(), m.group(2).strip()
        out.append((f"Шаблон отклика ({label}): {quote}", {"type": "response_template", "name": label}))
    return out


def chunks_from_briefs(sections: list[tuple[str, str]]) -> list[tuple[str, dict]]:
    out = []
    for title, body in sections:
        if title.startswith(BRIEF_PREFIX):
            service_name = title[len(BRIEF_PREFIX):].strip()
            out.append((f"{title}\n{body}", {"type": "brief_template", "name": service_name}))
    return out


def chunks_from_skills(body: str) -> list[tuple[str, dict]]:
    # Первая строка — реальный список навыков, вторая (в скобках) — внутренняя заметка, не нужна в черновике.
    first_line = body.splitlines()[0].strip()
    return [(f"Навыки: {first_line}", {"type": "skills", "name": "skills"})]


def build_index():
    text = FREELANCE_MD.read_text(encoding="utf-8")
    sections = _split_sections(text)
    section_map = dict(sections)

    all_chunks: list[tuple[str, dict]] = []
    all_chunks += chunks_from_services(section_map.get("Услуги (Кворки)", ""))
    all_chunks += chunks_from_portfolio(section_map.get("Публичные проекты (для откликов)", ""))
    all_chunks += chunks_from_templates(section_map.get("Шаблоны откликов", ""))
    all_chunks += chunks_from_skills(section_map.get("Навыки для профиля", ""))
    all_chunks += chunks_from_briefs(sections)

    if not all_chunks:
        raise RuntimeError("Ничего не извлеклось из FREELANCE.md — проверь структуру документа")

    model = SentenceTransformer(EMBEDDING_MODEL)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    if COLLECTION_NAME in [c.name for c in client.list_collections()]:
        client.delete_collection(COLLECTION_NAME)
    collection = client.create_collection(COLLECTION_NAME)

    ids = [f"{meta['type']}_{i}" for i, (_, meta) in enumerate(all_chunks)]
    texts = [t for t, _ in all_chunks]
    metadatas = [m for _, m in all_chunks]

    embeddings = model.encode(texts).tolist()
    collection.add(ids=ids, documents=texts, embeddings=embeddings, metadatas=metadatas)

    by_type = {}
    for _, m in all_chunks:
        by_type[m["type"]] = by_type.get(m["type"], 0) + 1
    print(f"Проиндексировано {len(texts)} чанков из {FREELANCE_MD}:")
    for t, n in by_type.items():
        print(f"  {t}: {n}")


if __name__ == "__main__":
    build_index()
