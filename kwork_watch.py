"""Новые заказы с биржи проектов Kwork (kwork.ru/projects).

RSS у Kwork нет, но страница биржи открывается без входа, а заказы лежат в ней JSON-ом («wants»):
полное описание, бюджет, «готов поднять до», сколько уже предложений. Берём первые страницы нужных разделов —
новые заказы сверху, за 2 минуты в разделе их появляется меньше, чем помещается на две страницы.
"""
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp

from fl_watch import Order

log = logging.getLogger("kwork_watch")
MSK = timezone(timedelta(hours=3))
# Разделы: 11 — Разработка и IT, 15 — Дизайн, 113 — Базы данных и клиентов (там парсинг)
CATEGORIES = {11: "Разработка и IT", 15: "Дизайн", 113: "Базы данных и клиентов"}
PAGES = 2
# Логотипы и векторную графику не берём: весь раздел «Логотип и брендинг» и такие заказы в других разделах (по заголовку —
# в описании «логотип есть» встречается и в заказах на сайт)
SKIP_CATEGORIES = {"25"}
SKIP_TITLE = re.compile(r"логотип|лого\b|логобук|вектор|svg|брендбук|айдентик|фирменн\w* стил|\blogo", re.I)


def skipped(w: dict) -> bool:
    return str(w.get("category_id")) in SKIP_CATEGORIES or bool(SKIP_TITLE.search(html.unescape(w.get("name") or "")))


def parse_page(page: str, cat_names: dict[str, str]) -> list[Order]:
    dec = json.JSONDecoder()
    for m in re.finditer(r'"wants":\[', page):
        try:
            arr, _ = dec.raw_decode(page[m.end() - 1:])
        except json.JSONDecodeError:
            continue
        if arr and isinstance(arr[0], dict) and "priceLimit" in arr[0]:
            break
    else:
        return []
    out = []
    for w in arr:
        if skipped(w):
            continue
        try:
            published = datetime.strptime(w.get("date_active") or w["date_create"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
        except (KeyError, ValueError):
            continue
        desc = html.unescape(w.get("description") or "").replace("\r\n", "\n").strip()
        budget = int(float(w.get("priceLimit") or 0)) or None
        max_budget = int(float(w.get("possiblePriceLimit") or 0)) or None
        out.append(Order(id=f"kw{w['id']}", title=html.unescape(w.get("name") or "").strip(),
                         link=f"https://kwork.ru/projects/{w['id']}/view", desc=desc,
                         category="Kwork / " + cat_names.get(str(w.get("category_id")), str(w.get("category_id"))),
                         budget=budget, for_all=True, published=published.astimezone(timezone.utc), source="kwork",
                         competitors=int(w.get("kwork_count") or 0), max_budget=max_budget))
    return out


def category_names(page: str) -> dict[str, str]:
    """Названия подразделов из того же JSON страницы (id → «Скрипты, боты и mini apps»)."""
    i = page.find('"categories":{')
    if i < 0:
        return {}
    try:
        cats, _ = json.JSONDecoder().raw_decode(page[i + len('"categories":'):])
    except json.JSONDecodeError:
        return {}
    names = {}
    for top in cats.values():
        for c in top.get("cats", []):
            names[str(c["CATID"])] = f"{top['name']} / {c['name']}"
    return names


async def fetch_all(session: aiohttp.ClientSession) -> list[Order]:
    orders: dict[str, Order] = {}
    names: dict[str, str] = {}
    for cat in CATEGORIES:
        for p in range(1, PAGES + 1):
            try:
                async with session.get(f"https://kwork.ru/projects?c={cat}&page={p}") as r:
                    if r.status != 200:
                        log.warning("Kwork %s/%s: HTTP %s", cat, p, r.status)
                        break
                    page = await r.text()
            except (aiohttp.ClientError, TimeoutError) as e:
                log.warning("Kwork %s/%s: %s", cat, p, e)
                break
            names = names or category_names(page)
            for o in parse_page(page, names):
                orders.setdefault(o.id, o)
    return sorted(orders.values(), key=lambda o: o.published)
