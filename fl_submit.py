"""Отклик на заказ FL.ru через ваш Chrome (в нём вы залогинены): заполнить форму → отправить.

Chrome должен быть запущен с портом отладки (start_chrome_fl.bat). API для откликов у FL.ru нет, поэтому
заполняем форму на странице заказа, как человек. Каждый отклик платный — отправка только по кнопке в Telegram
(«Утвердить и отправить»); предпросмотра нет (prepare() с скриншотом остался для ручных проверок).
"""
import json
import logging
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright

log = logging.getLogger("fl_submit")
CDP = os.getenv("CHROME_CDP") or "http://localhost:9333"
CHROME_EXE = os.getenv("CHROME_EXE") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_PROFILE = os.getenv("CHROME_PROFILE") or r"C:\Projects\_docs\fl-profile\.chrome-fl"


def chrome_up() -> bool:
    import socket
    port = int(CDP.rsplit(":", 1)[1].strip("/"))
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_chrome():
    """Тот же запуск, что start_chrome_fl.bat, но свёрнутым окном: Chrome бота закрыли — без него нет ТЗ, чатов, откликов."""
    import subprocess
    port = CDP.rsplit(":", 1)[1].strip("/")
    subprocess.Popen([CHROME_EXE, f"--remote-debugging-port={port}", f"--user-data-dir={CHROME_PROFILE}",
                      "--no-first-run", "--start-minimized", "https://www.fl.ru/"])
SHOTS = Path(__file__).parent / "shots"
PORTFOLIO = json.loads((Path(__file__).parent / "fl_portfolio.json").read_text(encoding="utf-8"))  # [{id, title}]


class NotReady(Exception):
    """Chrome не запущен / не залогинен / на заказ нельзя откликнуться — текст для пользователя."""


async def _page(p):
    try:
        browser = await p.chromium.connect_over_cdp(CDP, timeout=8000)
    except Exception:
        raise NotReady("Chrome для FL.ru не запущен. Запустите start_chrome_fl.bat (в папке бота) и войдите в FL.ru.")
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    return browser, await ctx.new_page()


async def _read_state(page) -> dict:
    t = await page.evaluate("() => document.body.innerText")
    left = re.search(r"Осталось откликов на заказы:\s*(\d+)", t)
    resp = re.search(r"Откликнулись:\s*(\d+)", t)
    prices = re.search(r"Цены:\s*от\s*([\d\s]+)\s*₽\s*до\s*([\d\s]+)\s*₽", t)
    terms = re.search(r"Сроки:\s*от\s*(\d+)\s*до\s*(\d+)", t)
    return {
        "left": int(left.group(1)) if left else None,
        "competitors": int(resp.group(1)) if resp else 0,
        "prices": tuple(int(x.replace(" ", "").replace("\xa0", "")) for x in prices.groups()) if prices else None,
        "terms": tuple(int(x) for x in terms.groups()) if terms else None,
        "logged_in": "Мои отклики" in t,
        "has_form": await page.locator("#newoffer #el-descr").count() > 0,
    }


async def _fill(page, text: str, price: int, days: int, works: list[int]):
    box = page.locator("#el-descr")
    await box.fill(text)
    # счётчик и проверки FL.ru слушают нажатия клавиш — «допечатываем» пробел и стираем его
    await box.press("End"); await box.type(" "); await box.press("Backspace")
    # поле фиксированной высоты — растягиваем, чтобы на скриншоте был виден весь текст
    await box.evaluate("e => { e.style.setProperty('height', (e.scrollHeight + 4) + 'px', 'important'); }")
    await page.locator("#el-time_from").fill(str(days))
    await page.locator("#el-cost_from").fill(str(price))
    if works and not await page.locator(f"#portfolio_work_{works[0]}").is_visible():
        await page.get_by_text("Добавить примеры работ", exact=True).click()   # список работ скрыт до этой кнопки
        await page.wait_for_timeout(1200)
    for wid in works[:3]:
        lab = page.locator(f"#portfolio_work_{wid}")
        if await lab.count():
            await lab.click()
            await page.wait_for_timeout(700)


async def prepare(link: str, text: str, price: int, days: int, works: list[int], order_id: str) -> dict:
    """Заполняет форму отклика, НЕ отправляет. Возвращает скриншот формы и статистику конкурентов."""
    async with async_playwright() as p:
        browser, page = await _page(p)
        try:
            await page.goto(link, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            st = await _read_state(page)
            if not st["logged_in"]:
                raise NotReady("В Chrome не выполнен вход в FL.ru — войдите и нажмите «Утвердить» ещё раз.")
            if not st["has_form"]:
                raise NotReady("На этот заказ откликнуться нельзя (уже откликнулись, заказ закрыт или только для PRO).")
            await _fill(page, text, price, days, works)
            chosen = await page.evaluate("""() => ['portf_id1','portf_id2','portf_id3']
                .map(n => (document.querySelector('[name='+n+']')||{}).value).filter(Boolean)""")
            SHOTS.mkdir(exist_ok=True)
            shot = SHOTS / f"{order_id}.png"
            # в кадр — текст, срок, цена и прикреплённые работы (без длинного списка портфолио ниже)
            top = await page.locator("#el-descr").bounding_box()
            slots = await page.locator("#work_block .works").bounding_box()
            await page.set_viewport_size({"width": 1280, "height": 1400})
            await page.evaluate("y => window.scrollTo(0, y)", max(0, (top or {"y": 0})["y"] - 40))
            await page.wait_for_timeout(500)
            # координаты — от начала страницы (full_page): длинный отклик растягивает поле выше окна,
            # и кадр по координатам окна вылезал за край → «Clipped area is … outside the resulting image»
            sy = await page.evaluate("() => window.scrollY")
            top = await page.locator("#el-descr").bounding_box()
            slots = await page.locator("#work_block .works").bounding_box()
            if not slots or slots["height"] <= 0 or slots["y"] < top["y"]:
                slots = top                                  # блок работ скрыт — кадр по полю текста
            height = max(200, slots["y"] + slots["height"] - top["y"] + 180)
            try:
                await page.screenshot(path=str(shot), full_page=True, clip={
                    "x": max(0, top["x"] - 20), "y": max(0, top["y"] + sy - 80), "width": 760, "height": height})
            except Exception:
                log.warning("скриншот формы по кадру не вышел — снимаю страницу целиком", exc_info=True)
                await page.screenshot(path=str(shot), full_page=True)
            return {**st, "shot": str(shot), "works": [int(x) for x in chosen]}
        finally:
            await page.close()          # закрываем только свою вкладку, ваш Chrome остаётся
            await browser.close()       # для CDP это отключение, а не закрытие браузера


XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}


async def offer_exists_ctx(ctx, project_id: str) -> bool | None:
    """Есть ли наш отклик на заказ — по JSON-списку откликов FL.ru (не зависит от вёрстки страницы «Мои отклики»).
    True/False; None — проверить не вышло (FL.ru недоступен, сессия слетела)."""
    try:
        for dialogues in (0, 1):               # отклики без переписки и с перепиской — в разных списках
            r = await ctx.request.get(f"https://www.fl.ru/projects/offers/?limit=50&dialogues={dialogues}"
                                      "&deleted=0&sort=lastMessage&offset=0", headers=XHR, timeout=30000)
            if any(str(i.get("project_id")) == str(project_id) for i in (await r.json())["items"]):
                return True
        return False
    except Exception:
        log.warning("проверка откликов FL.ru не удалась", exc_info=True)
        return None


async def offer_exists(project_id: str) -> bool | None:
    """То же, но с собственным подключением к Chrome (когда отправка зависла или бот перезапускался)."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(CDP, timeout=8000)
            try:
                return await offer_exists_ctx(browser.contexts[0], project_id)
            finally:
                await browser.close()
    except Exception:
        log.warning("не подключиться к Chrome для проверки отклика", exc_info=True)
        return None


async def submit(link: str, text: str, price: int, days: int, works: list[int], project_id: str) -> dict:
    """Заполняет форму и сразу отправляет (предпросмотра нет). Успех подтверждает по списку откликов FL.ru.
    Каждый шаг пишется в лог: если отправка снова зависнет, видно, на каком месте."""
    async with async_playwright() as p:
        browser, page = await _page(p)
        try:
            page.set_default_timeout(25000)                # любое ожидание — не дольше 25 с, а не «вечно»
            log.info("отправка %s: открываю заказ", project_id)
            await page.goto(link, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            st = await _read_state(page)
            if not st["logged_in"]:
                raise NotReady("В Chrome не выполнен вход в FL.ru — войдите и нажмите «Утвердить» ещё раз.")
            if not st["has_form"]:
                if await offer_exists_ctx(page.context, project_id):
                    return {"ok": True, "left": st["left"], "competitors": st["competitors"], "already": True}
                raise NotReady("На этот заказ откликнуться нельзя (заказ закрыт, только для PRO или лимит откликов).")
            log.info("отправка %s: заполняю форму", project_id)
            await _fill(page, text, price, days, works)
            log.info("отправка %s: нажимаю «Отправить отклик»", project_id)
            await page.get_by_role("button", name="Отправить отклик").click()
            await page.wait_for_timeout(4000)
            err = await page.evaluate("""() => [...document.querySelectorAll('[id$=-error-text]')]
                .map(e => e.innerText.trim()).filter(Boolean)""")
            if err:
                raise NotReady("FL.ru не принял отклик: " + "; ".join(err))
            log.info("отправка %s: проверяю, что отклик появился", project_id)
            ok = await offer_exists_ctx(page.context, project_id)
            if ok is None:                                  # JSON недоступен — запасной путь через страницу «Мои отклики»
                await page.goto("https://www.fl.ru/projects/my-offers/", wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                ok = f"/projects/{project_id}/" in await page.evaluate("() => document.body.innerHTML")
            after = await _read_state(page) if "my-offers" not in page.url else st
            log.info("отправка %s: готово, ok=%s", project_id, ok)
            return {"ok": bool(ok), "left": after["left"], "competitors": st["competitors"]}
        finally:
            await page.close()
            await browser.close()


def portfolio_menu() -> str:
    """Нумерованный список работ портфолио для промпта черновика."""
    return "\n".join(f"{i}. {w['title']}" for i, w in enumerate(PORTFOLIO, 1))


def works_from_numbers(nums: list[int]) -> list[int]:
    return [PORTFOLIO[n - 1]["id"] for n in nums if 1 <= n <= len(PORTFOLIO)][:3]
