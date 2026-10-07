"""Вложения к заказу (ТЗ в Word/PDF/Excel, скриншоты) → материалы для нейросети, которая пишет отклик.

Файлы видны и скачиваются только залогиненному пользователю (FL.ru показывает блок вложений лишь после входа,
Kwork отдаёт файл только с сессией), поэтому качаем через ваш Chrome (тот же, что для откликов).
Скачанное лежит в att/<номер заказа>/ — при правке черновика файлы берутся оттуда, повторно не качаем.

Word и Excel превращаем в текст сами (это zip с XML — библиотеки не нужны), PDF и картинки нейросеть читает сама.
"""
import base64
import html
import logging
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote

from playwright.async_api import async_playwright

from fl_submit import CDP

log = logging.getLogger("attachments")
DIR = Path(__file__).parent / "att"
MAX_FILES = 6
MAX_BYTES = 15 * 1024 * 1024
MAX_TEXT = 40_000                     # символов текста из всех файлов вместе
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
TEXTS = {".txt", ".csv", ".md", ".json", ".xml", ".html", ".htm", ".rtf"}


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", unquote(name)).strip()[:120] or "file"


def docx_text(path: Path) -> str:
    """Текст Word: абзацы и ячейки таблиц по строкам."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"<w:tab/>", "<w:t> </w:t>", xml)
    xml = re.sub(r"</w:tc>", "<w:t> | </w:t>", xml)            # ячейки таблицы — через « | »
    out = []
    for p in re.split(r"</w:p>|</w:tr>", xml):
        t = "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", p))
        if t.strip(" |"):
            out.append(html.unescape(t).strip(" |"))
    return "\n".join(out)


def xlsx_text(path: Path) -> str:
    """Текст Excel: листы → строки, ячейки через « | »."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            shared = [html.unescape("".join(re.findall(r"<t[^>]*>([^<]*)</t>", si))) for si in re.findall(r"<si>(.*?)</si>", sx, re.S)]
        out = []
        for sheet in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
            sx = z.read(sheet).decode("utf-8", "ignore")
            out.append(f"[лист {sheet.rsplit('/', 1)[1][5:-4]}]")
            for row in re.findall(r"<row[^>]*>(.*?)</row>", sx, re.S):
                cells = []
                for attrs, body in re.findall(r"<c([^>]*)>(.*?)</c>", row, re.S):
                    v = re.search(r"<v>([^<]*)</v>", body) or re.search(r"<t[^>]*>([^<]*)</t>", body)
                    if not v:
                        continue
                    val = v.group(1)
                    if 't="s"' in attrs and val.isdigit() and int(val) < len(shared):
                        val = shared[int(val)]
                    cells.append(html.unescape(val))
                if any(c.strip() for c in cells):
                    out.append(" | ".join(cells))
    return "\n".join(out)


def to_blocks(paths: list[Path]) -> tuple[list[dict], list[str], list[str]]:
    """Файлы → блоки сообщения для Claude. Возвращает (блоки, что прочитано, что пропущено и почему)."""
    blocks, read, skipped, budget = [], [], [], MAX_TEXT
    for p in paths:
        ext = p.suffix.lower()
        try:
            if ext == ".docx" or ext in (".xlsx", ".xlsm") or ext in TEXTS:
                if ext == ".docx":
                    text = docx_text(p)
                elif ext in (".xlsx", ".xlsm"):
                    text = xlsx_text(p)
                else:
                    raw = p.read_bytes()
                    text = raw.decode("utf-8") if raw[:3] != b"\xef\xbb\xbf" else raw[3:].decode("utf-8")
                if not text.strip() or budget <= 0:
                    skipped.append(f"{p.name} (пусто)" if not text.strip() else f"{p.name} (не влез)")
                    continue
                cut = text[:budget]
                budget -= len(cut)
                blocks.append({"type": "text", "text": f"=== Вложение «{p.name}» ===\n{cut}"
                                                       + ("\n[…обрезано]" if len(cut) < len(text) else "")})
            elif ext == ".pdf":
                blocks.append({"type": "text", "text": f"=== Вложение «{p.name}» (PDF ниже) ==="})
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                              "data": base64.b64encode(p.read_bytes()).decode()}})
            elif ext in IMAGES:
                if p.stat().st_size > 5 * 1024 * 1024:
                    skipped.append(f"{p.name} (картинка больше 5 МБ)")
                    continue
                blocks.append({"type": "text", "text": f"=== Вложение «{p.name}» (картинка ниже) ==="})
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": IMAGES[ext],
                                                           "data": base64.b64encode(p.read_bytes()).decode()}})
            else:
                skipped.append(f"{p.name} (формат {ext or 'без расширения'} не читаю)")
                continue
            read.append(p.name)
        except Exception as e:                       # битый файл не должен ломать черновик
            log.warning("вложение %s: %s", p, e)
            skipped.append(f"{p.name} (не открылся)")
    return blocks, read, skipped


def cached(order_id: str) -> list[Path]:
    d = DIR / order_id
    return sorted(x for x in d.iterdir() if x.is_file()) if d.exists() else []


async def download(o) -> dict:
    """Скачивает вложения заказа в att/<id>/. Для FL.ru сначала находит их на странице заказа.
    Возвращает {"files": [...имена], "error": текст или None}. Chrome не запущен — error, черновик без ТЗ."""
    found = [{"name": f["name"], "url": f["url"]} for f in (getattr(o, "files", None) or [])]
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(CDP, timeout=8000)
            except Exception:
                return {"files": [], "error": "Chrome не запущен — вложения не прочитаны"}
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            try:
                if o.source == "fl":
                    page = await ctx.new_page()
                    try:
                        await page.goto(o.link, wait_until="domcontentloaded")
                        await page.wait_for_timeout(1500)
                        found = await page.evaluate("""() => [...document.querySelectorAll(
                            '.base-attach-class a[href*="/download/"], a[href*="fl.ru/download/files/"]')]
                            .map(a => ({name: a.innerText.trim(), url: a.href}))""")
                    finally:
                        await page.close()
                if not found:
                    return {"files": [], "error": None}
                d = DIR / o.id
                d.mkdir(parents=True, exist_ok=True)
                names = []
                for f in found[:MAX_FILES]:
                    name = safe_name(f["name"] or f["url"].rsplit("/", 1)[-1])
                    r = await ctx.request.get(f["url"], timeout=30000)
                    body = await r.body()
                    if r.status != 200 or body[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
                        log.warning("вложение %s: HTTP %s / страница вместо файла", f["url"], r.status)
                        continue
                    if len(body) > MAX_BYTES:
                        continue
                    (d / name).write_bytes(body)
                    names.append(name)
                extra = len(found) - MAX_FILES
                return {"files": names, "error": f"ещё {extra} файлов не скачаны (лимит {MAX_FILES})" if extra > 0 else None}
            finally:
                await browser.close()       # для CDP — отключение, ваш Chrome остаётся
    except Exception as e:
        log.exception("вложения %s", o.id)
        return {"files": [], "error": f"вложения не скачались ({type(e).__name__})"}
