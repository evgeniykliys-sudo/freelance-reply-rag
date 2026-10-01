"""Печатает сырые дистанции поиска на разнородных примерах — использовался для
калибровки MAX_RELEVANT_DISTANCE в rag.py. Не автотест, ручная проверка на глаз."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from rag import search

CASES = {
    "parser (должен совпасть)": "Нужен парсер цен конкурентов с нескольких интернет-магазинов, раз в день, результат в гугл-таблицу",
    "booking (должен совпасть)": "Ищу разработчика для телеграм бота записи клиентов в барбершоп, с базой и уведомлениями",
    "ai-dept (погранично)": (
        "Ищу технического специалиста в команду для развития проекта AI-директорат — "
        "системы AI-агентов для собственника бизнеса. Работа с несколькими AI-агентами, "
        "интеграции CRM/финансы/отчёты, созвоны 2-3 раза в неделю."
    ),
    "mcp (должен совпасть)": "Нужен MCP-сервер для интеграции с Wildberries и Ozon API, production-grade, с аудитом и rate limiting",
    "unrelated (не должен совпасть)": "Нужен дизайнер логотипа для кофейни, в стиле минимализм, 3 варианта на выбор",
}


def main():
    for name, text in CASES.items():
        print("=" * 70)
        print(name, ":", text[:70])
        for c in search(text, top_k=5):
            print(f"  dist={c['distance']:.3f}  [{c['type']}] {c['name']}")


if __name__ == "__main__":
    main()
