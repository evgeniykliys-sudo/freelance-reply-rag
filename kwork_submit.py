"""Предложение на заказ биржи Kwork через ваш Chrome (тот же, что для FL.ru): заполнить форму → отправить.

Форма — kwork.ru/new_offer?project=ID: описание (150–2000 символов), стоимость (Kwork задаёт минимум для заказа),
порядок оплаты и срок из списка. Отправка — только по кнопке в Telegram («Утвердить и отправить»);
предпросмотра нет (prepare() со скриншотом остался для ручных проверок).
"""
import logging
import re

from playwright.async_api import async_playwright

from fl_submit import CDP, SHOTS, NotReady

log = logging.getLogger("kwork_submit")

TERMS = {"недел": 7, "месяц": 30}


def term_days(label: str) -> int:
    """«5 дней» → 5, «2 недели» → 14, «1 месяц» → 30."""
    n = int(re.match(r"\d+", label.strip()).group(0))
    return n * next((v for k, v in TERMS.items() if k in label), 1)


def pick_term(labels: list[str], days: int) -> str:
    """Ближайший срок не меньше нужного (или самый длинный)."""
    ok = [x for x in labels if term_days(x) >= days]
    return min(ok, key=term_days) if ok else max(labels, key=term_days)


def project_id(link: str) -> str:
    return re.search(r"/projects/(\d+)", link).group(1)


async def _page(p):
    try:
        browser = await p.chromium.connect_over_cdp(CDP, timeout=8000)
    except Exception:
        raise NotReady("Chrome не запущен. Запустите start_chrome_fl.bat (в папке бота) — Kwork залогинен в нём же.")
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    return browser, await ctx.new_page()


async def _open(page, link: str) -> dict:
    await page.goto(f"https://kwork.ru/new_offer?project={project_id(link)}", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    t = await page.evaluate("() => document.body.innerText")
    if "Регистрация" in t[:800]:
        raise NotReady("В Chrome не выполнен вход в Kwork — войдите и нажмите «Утвердить» ещё раз.")
    if not await page.locator(".trumbowyg-editor:visible").count():
        raise NotReady("На этот заказ предложить услугу нельзя (уже предложили, заказ закрыт или снят).")
    rng = await page.locator("input[type=tel]:visible").first.get_attribute("placeholder") or ""
    nums = [int(x.replace(" ", "").replace("\xa0", "")) for x in re.findall(r"\d[\d \xa0]*", rng)]
    comp = re.search(r"Предложений:\s*(\d+)", t)
    return {"min": nums[0] if nums else None, "max": nums[1] if len(nums) > 1 else None,
            "competitors": int(comp.group(1)) if comp else 0}


async def _put(page, ed, text: str):
    await ed.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            await page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            await page.keyboard.press("Enter")
    # счётчик и проверка стоп-слов слушают клавиатуру — «допечатываем» пробел и стираем
    await page.keyboard.type(" ")
    await page.keyboard.press("Backspace")


async def _fill(page, text: str, price: int, days: int, title: str) -> dict:
    await _put(page, page.locator(".trumbowyg-editor:visible").first, text)
    pr =page.locator("input[type=tel]:visible").first
    await pr.click()
    await pr.fill("")
    await pr.type(str(price))
    pay = page.get_by_text("Целиком, когда заказ выполнен", exact=True)
    if await pay.count():
        await pay.first.click()
        await page.wait_for_timeout(700)
    # при оплате целиком Kwork просит название заказа (до 70 символов)
    name = page.locator(".trumbowyg-editor[placeholder='Введите название заказа']:visible")
    if await name.count():
        await _put(page, name.first, title[:70].strip())
    await page.locator(".vs__actions:visible").first.click()
    await page.wait_for_timeout(700)
    labels = [x.strip() for x in await page.locator(".vs__dropdown-option").all_inner_texts()]
    term = pick_term(labels, days)
    await page.locator(".vs__dropdown-option").filter(has_text=re.compile(rf"^\s*{re.escape(term)}\s*$")).first.click()
    await page.wait_for_timeout(800)
    t = await page.evaluate("() => document.body.innerText")
    chars = re.search(r"(\d+) из 2000 символов", t)
    return {"term": term, "chars": int(chars.group(1)) if chars else len(text),
            "stopwords": "контактн" in t.lower() and "запрещ" in t.lower()}


def fit_price(price: int, st: dict) -> int:
    lo, hi = st.get("min"), st.get("max")
    if lo and price < lo:
        return lo
    if hi and price > hi:
        return hi
    return price


async def prepare(link: str, text: str, price: int, days: int, title: str, order_id: str) -> dict:
    """Заполняет форму предложения, НЕ отправляет. Возвращает скриншот и итоговые цену/срок."""
    if len(text) < 150:
        raise NotReady(f"Kwork требует от 150 символов, в черновике {len(text)} — допиши через «Править».")
    async with async_playwright() as p:
        browser, page = await _page(p)
        try:
            await page.set_viewport_size({"width": 1280, "height": 1600})
            st = await _open(page, link)
            st["price"] = fit_price(price, st)
            st.update(await _fill(page, text[:2000], st["price"], days, title))
            SHOTS.mkdir(exist_ok=True)
            shot = SHOTS / f"{order_id}.png"
            top = await page.get_by_text("Описание", exact=True).first.bounding_box()
            btn = await page.locator("button:visible, .kw-button:visible").filter(has_text="Предложить").last.bounding_box()
            await page.screenshot(path=str(shot), full_page=True,
                                  clip={"x": top["x"] - 20, "y": top["y"] - 20, "width": 700,
                                        "height": btn["y"] + btn["height"] - top["y"] + 40})
            return {**st, "shot": str(shot)}
        finally:
            await page.close()          # закрываем только свою вкладку
            await browser.close()       # для CDP это отключение, а не закрытие браузера


async def _offer_sent(page, link: str) -> bool:
    """Отправилось — на странице заказа больше нет «Предложить услугу» (или есть «Ваше предложение»)."""
    await page.goto(link, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    t = await page.evaluate("() => document.body.innerText")
    return "Предложить услугу" not in t or "Ваше предложение" in t


async def offer_exists(link: str) -> bool | None:
    """Есть ли наше предложение на заказе — со своим подключением к Chrome (отправка зависла / бот перезапускался)."""
    try:
        async with async_playwright() as p:
            browser, page = await _page(p)
            try:
                page.set_default_timeout(25000)
                return await _offer_sent(page, link)
            finally:
                await page.close()
                await browser.close()
    except Exception:
        log.warning("проверка предложения Kwork не удалась", exc_info=True)
        return None


async def submit(link: str, text: str, price: int, days: int, title: str) -> dict:
    """Заполняет форму и сразу отправляет (предпросмотра нет). Если Kwork подсветил контакты/стоп-слова — не отправляет.
    Каждый шаг пишется в лог: если отправка зависнет, видно, на каком месте."""
    if len(text) < 150:
        raise NotReady(f"Kwork требует от 150 символов, в черновике {len(text)} — допиши через «Править».")
    async with async_playwright() as p:
        browser, page = await _page(p)
        try:
            page.set_default_timeout(25000)                # любое ожидание — не дольше 25 с, а не «вечно»
            pid = project_id(link)
            log.info("отправка Kwork %s: открываю форму", pid)
            st = await _open(page, link)
            price = fit_price(price, st)
            log.info("отправка Kwork %s: заполняю форму", pid)
            filled = await _fill(page, text[:2000], price, days, title)
            if filled.get("stopwords"):
                raise NotReady("Kwork подсветил в тексте контакты/стоп-слова и не пропустит предложение — "
                               "поправь текст через «Править» и утверди ещё раз.")
            log.info("отправка Kwork %s: нажимаю «Предложить»", pid)
            await page.locator("button:visible, .kw-button:visible").filter(has_text="Предложить").last.click()
            await page.wait_for_timeout(4000)
            errors = await page.evaluate("""() => [...document.querySelectorAll('.error, [class*=error]')]
                .filter(e => e.offsetParent && e.innerText.trim()).map(e => e.innerText.trim().slice(0, 150))""")
            if "new_offer" in page.url and errors:
                raise NotReady("Kwork не принял предложение: " + "; ".join(errors[:3]))
            log.info("отправка Kwork %s: проверяю, что предложение появилось", pid)
            ok = await _offer_sent(page, link)
            log.info("отправка Kwork %s: готово, ok=%s", pid, ok)
            return {"ok": ok, "price": price, "url": page.url, "term": filled["term"], "competitors": st["competitors"]}
        finally:
            await page.close()
            await browser.close()
