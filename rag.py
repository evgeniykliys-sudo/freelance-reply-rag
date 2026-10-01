import os
from pathlib import Path

import chromadb
from anthropic import Anthropic
from sentence_transformers import SentenceTransformer

CHROMA_DIR = Path(__file__).parent / "chroma_db"
COLLECTION_NAME = "freelance"
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
CLAUDE_MODEL = os.getenv("RAG_MODEL") or "claude-sonnet-5"
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


def _get_claude() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
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
        max_tokens=700,
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
