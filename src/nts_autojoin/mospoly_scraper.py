# -*- coding: utf-8 -*-
import json, re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Any, Iterable, Optional
from datetime import datetime
from dateutil import tz

from playwright.async_api import async_playwright

try:
    from .settings import get_logs_dir
except ImportError:
    from nts_autojoin.settings import get_logs_dir

MOSPOLY_URL = "https://e.mospolytech.ru/#/schedule/current"

@dataclass
class WebEvent:
    title: str
    start: str      # "HH:MM"
    end: str        # "HH:MM"
    href: str
    day_str: str    # "Пн"/"Вт"/...

TIME_RE = re.compile(r'(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})')

def _first_nonempty(lines: Iterable[str]) -> str:
    for ln in lines:
        s = ln.strip()
        if s:
            return s
    return ""

def _norm_same_site(v: Optional[str]) -> Optional[str]:
    """Приводим sameSite к ожидаемым Playwright значениям."""
    if not v:
        return None
    v = str(v).strip().lower()
    if v in ("none", "no_restriction"):
        return "None"
    if v == "lax":
        return "Lax"
    if v == "strict":
        return "Strict"
    # "unspecified" и прочие — просто не передаём поле
    return None

def _norm_expires(e) -> int:
    """Конвертируем expirationDate/expires из float/str → int, иначе -1 (сессионная)."""
    if e is None:
        return -1
    try:
        return int(float(e))
    except Exception:
        return -1

async def _load_cookies_from_file(context, cookie_path: Path, only_domain: str = "e.mospolytech.ru"):
    """
    Поддерживает:
      1) Playwright storage state: { "cookies": [ ... ] }
      2) Массив cookies как из DevTools/расширения.
    Фильтруем по домену (оставляем только e.mospolytech.ru и его поддомен).
    Нормализуем sameSite.
    """
    raw = json.loads(cookie_path.read_text(encoding="utf-8"))
    cookies: List[Dict[str, Any]] = []

    def _accept(domain: str) -> bool:
        d = domain or ""
        return "e.mospolytech.ru" in d  # e.mospolytech.ru или .e.mospolytech.ru

    if isinstance(raw, dict) and "cookies" in raw:
        it = raw["cookies"]
        for c in it:
            if not _accept(c.get("domain","")):
                continue
            out = {
                "name":  c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".e.mospolytech.ru"),
                "path": c.get("path", "/"),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
            }
            ss = _norm_same_site(c.get("sameSite"))
            if ss:
                out["sameSite"] = ss
            # session cookie → expires = -1
            if c.get("session") is True:
                out["expires"] = -1
            else:
                out["expires"] = _norm_expires(c.get("expirationDate", c.get("expires")))
            cookies.append(out)

    elif isinstance(raw, list):
        for c in raw:
            if not _accept(c.get("domain","")):
                continue
            out = {
                "name":  c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".e.mospolytech.ru"),
                "path": c.get("path", "/"),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
            }
            ss = _norm_same_site(c.get("sameSite"))
            if ss:
                out["sameSite"] = ss
            if c.get("session") is True:
                out["expires"] = -1
            else:
                out["expires"] = _norm_expires(c.get("expirationDate", c.get("expires")))
            cookies.append(out)
    else:
        raise ValueError("Не понял формат cookies.json")

    if cookies:
        await context.add_cookies(cookies)

async def fetch_mospoly_schedule(
    cookies_file: str,
    headless: bool = False,
    chromium_executable: Optional[str] = None
) -> List[WebEvent]:
    """
    Возвращает список событий с 'Webinar' (ссылка, время, заголовок).
    Для системного Chrome в headless добавляем '--headless=new'.
    """
    out: List[WebEvent] = []
    async with async_playwright() as pw:
        # параметры запуска
        launch_kwargs: dict = {"headless": headless}
        if chromium_executable:
            launch_kwargs["executable_path"] = chromium_executable
            if headless:
                launch_kwargs["args"] = ["--headless=new"]
        else:
            launch_kwargs["channel"] = "chrome"
            if headless:
                launch_kwargs["args"] = ["--headless=new"]

        # фоллбеки
        try:
            browser = await pw.chromium.launch(**launch_kwargs)
        except Exception:
            try:
                browser = await pw.chromium.launch(headless=headless, channel="chrome",
                                                   args=(["--headless=new"] if headless else None))
            except Exception:
                browser = await pw.chromium.launch(headless=headless)

        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        try:
            if cookies_file:
                await _load_cookies_from_file(context, Path(cookies_file))

            page = await context.new_page()
            await page.goto(MOSPOLY_URL, wait_until="domcontentloaded", timeout=90_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass

            await page.wait_for_timeout(1500)

            anchors = page.locator("a:has-text('Webinar')")
            cnt = await anchors.count()
            if cnt == 0:
                await page.mouse.wheel(0, 1200)
                await page.wait_for_timeout(1500)
                cnt = await anchors.count()

            for i in range(cnt):
                a = anchors.nth(i)
                href = await a.get_attribute("href") or ""

                container = a.locator("xpath=ancestor::*[self::div or self::article][1]")
                text = await container.inner_text()

                m = TIME_RE.search(text)
                start, end = ("", "")
                if m:
                    start, end = m.group(1), m.group(2)

                lines = [ln.strip() for ln in text.splitlines()]
                lines = [ln for ln in lines if ln and "webinar" not in ln.lower()]
                if lines and TIME_RE.match(lines[0]):
                    title = _first_nonempty(lines[1:])
                else:
                    title = _first_nonempty(lines)

                day_str = ""
                try:
                    day_el = a.locator("xpath=ancestor::div[1]/preceding::div[contains(.,'Пн') or contains(.,'Вт') or contains(.,'Ср') or contains(.,'Чт') or contains(.,'Пт') or contains(.,'Сб') or contains(.,'Вс')][1]")
                    day_str = (await day_el.inner_text()).strip().split()[0]
                except Exception:
                    pass

                if href and start and end and title:
                    out.append(WebEvent(title=title, start=start, end=end, href=href, day_str=day_str))

            await context.close()
            await browser.close()
        finally:
            try: await context.close()
            except Exception: pass
            try: await browser.close()
            except Exception: pass

    return out

def to_yaml_mapping(events: List[WebEvent]) -> str:
    import yaml
    arr: List[Dict[str, Any]] = []
    for e in events:
        arr.append({
            "name": e.title,
            "url": e.href,
            "hint_time": f"{e.day_str} {e.start}-{e.end}",
        })
    return yaml.safe_dump(arr, allow_unicode=True, sort_keys=False)

if __name__ == "__main__":
    import asyncio, argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", required=True)
    ap.add_argument("--headless", action="store_true", default=False)
    ap.add_argument("--chrome", default=None)
    ap.add_argument("--save", default=None)
    args = ap.parse_args()

    get_logs_dir().mkdir(parents=True, exist_ok=True)
    events = asyncio.run(fetch_mospoly_schedule(args.cookies, headless=args.headless, chromium_executable=args.chrome))
    print(f"Найдено событий: {len(events)}")
    data = [asdict(e) for e in events]
    save_path = Path(args.save) if args.save else get_logs_dir() / "mospoly_schedule.json"
    save_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON сохранён: {args.save}")

    print("\n--- YAML фрагмент (для meetings) ---")
    print(to_yaml_mapping(events))
