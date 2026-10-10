import os
from pathlib import Path

import chromadb
from anthropic import Anthropic
from sentence_transformers import SentenceTransformer

CHROMA_DIR = Path(__file__).parent / "chroma_db"
COLLECTION_NAME = "freelance"
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
CLAUDE_MODEL = os.getenv("RAG_MODEL") or "claude-sonnet-5-5"
TOP_K = 10
# ChromaDB по умолчанию использует squared L2 (не косинус 0-2!). Порог откалиброван
# эмпирически на реальных примерах (см. scripts/calibrate.py): явные совпадения по
# профилю — дистанция ~9-14, заказы вне профиля (дизайн, не-IT) — от ~19.5.
MAX_RELEVANT_DISTANCE = 16.0

SYSTEM_PROMPT = """Ты помогаешь фрилансеру Евгению (Junior, Python/Telegram-боты, vibe \
coding через AI-инструменты) быстро составлять черновик отклика на заказ с Kwork/FL.ru \
или из Telegram-канала.

Тебе дают текст заказа и фрагменты из его базы услуг/портфолио/шаблонов откликов \
(найдены поиском по смыслу — могут быть неточными или не по теме). Работай строго по \
следующим правилам:

1. Используй ТОЛЬКО услуги, цены, бриф-вопросы и ссылки на портфолио, которые реально \
есть в переданных фрагментах. Никогда не придумывай цену или услугу, которых там нет.

2. Если среди фрагментов есть явный отказ от какого-то вида работ (обход защиты, капча, \
многоаккаунтность и т.п.) и заказ просит именно это — не пиши продающий отклик, а прямо \
скажи, что это не тот тип задач, которые Евгений берёт, и предложи вежливо отказаться.

3. Если ни один фрагмент не похож на подходящую услугу (заказ вне текущего профиля — не \
Python/боты/автоматизация) — не притягивай услугу за уши. Напиши одну фразу: что заказ не \
подходит под текущий профиль услуг, и почему, без черновика отклика.

4. Если подходящая услуга есть — напиши черновик отклика в установленном стиле: \
приветствие по делу, короткое подтверждение релевантного опыта со ссылкой на конкретный \
проект портфолио (если он есть во фрагментах), затем один уточняющий вопрос из \
бриф-шаблона (самый важный для оценки, не весь список). Без канцелярита и без "дорогой \
заказчик". 3-5 предложений. Если в заказе теоретически подходят две услуги — выбери одну, \
более точную, не сваливай всё в один ответ.

5. В конце отдельной строкой — пометка для Евгения (не часть отклика): какая услуга и цена \
из прайса использована, и что ответ лучше проверить глазами перед отправкой.

Отвечай по-русски."""


_embedder = None
_client = None
_collection = None


def _get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBEDDING_MODEL)
    return _embedder


def _get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _collection = client.get_collection(COLLECTION_NAME)
    return _collection


def cached(system: str) -> list[dict]:
    """Системный промпт с пометкой для кэша: правила одинаковые в каждом запросе, из кэша они в ~20 раз дешевле."""
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


# цены $ за миллион токенов: вход, выход, запись в кэш, чтение из кэша (claude.com/pricing, октябрь 2026)
PRICES = {"claude-sonnet-5-5": (2, 10, 2.5, 0.1), "claude-sonnet-5": (2, 10, 2.5, 0.2),
          "claude-haiku-5-5": (0.1, 0.5, 0.125, 0.01), "claude-haiku-4-5-20251001": (1, 5, 1.25, 0.1)}
USAGE_DB = Path(__file__).parent / "fl_seen.db"


def cost_usd(model: str, inp: int, out: int, cw: int, cr: int) -> float:
    p = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    return (inp * p[0] + out * p[1] + cw * p[2] + cr * p[3]) / 1e6


def _log_usage(model: str, u):
    """Каждый запрос к API — строка в api_usage: по ней бот считает реальный расход (команда /cost)."""
    try:
        import sqlite3
        from datetime import datetime
        row = (model, u.input_tokens, u.output_tokens, u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0)
        c = sqlite3.connect(USAGE_DB)
        c.execute("create table if not exists api_usage (at text, model text, inp int, out int, cw int, cr int, usd real)")
        c.execute("insert into api_usage values (?,?,?,?,?,?,?)",
                  (datetime.now().isoformat(timespec="seconds"), *row, cost_usd(*row)))
        c.commit()
        c.close()
    except Exception:
        pass                                   # учёт не должен ломать черновики


def _get_claude() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        create = _client.messages.create

        def counted(**kw):
            r = create(**kw)
            _log_usage(kw.get("model", ""), r.usage)
            return r
        _client.messages.create = counted
    return _client


def search(posting: str, top_k: int = TOP_K) -> list[dict]:
    embedder = _get_embedder()
    collection = _get_collection()

    query_embedding = embedder.encode([posting]).tolist()
    results = collection.query(query_embeddings=query_embedding, n_results=top_k)

    chunks = []
    for text, meta, distance in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        chunks.append({"text": text, "type": meta["type"], "name": meta["name"], "distance": distance})
    return chunks


def draft_reply(posting: str) -> tuple[str, list[dict]]:
    chunks = search(posting)
    relevant = [c for c in chunks if c["distance"] <= MAX_RELEVANT_DISTANCE]

    if not relevant:
        return (
            "Ни один фрагмент базы услуг не похож на этот заказ по смыслу — похоже, он вне "
            "текущего профиля (Python/Telegram-боты/автоматизация). Черновик не составлен, "
            "чтобы не притягивать несуществующую услугу.",
            chunks,
        )

    context = "\n\n".join(f"[{c['type']}] {c['text']}" for c in relevant)
    response = _get_claude().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Фрагменты базы услуг/портфолио/шаблонов:\n\n{context}\n\nТекст заказа:\n{posting}",
            }
        ],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, relevant
