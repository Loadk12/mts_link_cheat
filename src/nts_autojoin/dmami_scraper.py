# -*- coding: utf-8 -*-
"""
Скрапер rasp.dmami.ru для группового расписания:
 - вводит номер группы «реактивно» (нативный сеттер + события),
 - кликает появившуюся кнопку группы,
 - автоскроллит, ждёт дорисовку,
 - собирает все my.mts-link.ru-ссылки с временем, названием и днём недели.

Работает без куков/логина. Можно headless.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any

from playwright.async_api import async_playwright, Page

try:
    from .settings import get_logs_dir
except ImportError:
    from nts_autojoin.settings import get_logs_dir

DMAMI = "https://rasp.dmami.ru"
TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})")


@dataclass
class WebEvent:
    day: str
    start: str
    end: str
    title: str
    href: str


# ---------- helpers ----------


async def _react_fill_and_trigger(page: Page, selector: str, value: str) -> None:
    """Заполняем input правильно для реактивного фронта и шлём события."""
    await page.wait_for_selector(selector, timeout=10_000)
    await page.click(selector, timeout=10_000)
    await page.evaluate(
        """({ sel, val }) => {
            const el = document.querySelector(sel);
            if (!el) return;
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            el.focus();
            setter.call(el, val);
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup',   { bubbles: true, key: 'Enter' }));
            el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter' }));
        }""",
        {"sel": selector, "val": value},
    )


async def _auto_scroll(
    page: Page, max_steps: int = 20, step_px: int = 900, pause_ms: int = 300
) -> None:
    """Проматываем вниз/вверх, чтобы ленивые блоки дорисовались."""
    for _ in range(max_steps):
        await page.mouse.wheel(0, step_px)
        await page.wait_for_timeout(pause_ms)
    for _ in range(3):
        await page.mouse.wheel(0, -step_px)
        await page.wait_for_timeout(pause_ms)


def _split_time(t: str) -> tuple[str, str]:
    t = (t or "").strip()
    m = TIME_RE.search(t)
    if not m:
        return "", ""
    return m.group(1), m.group(2)


# ---------- public API ----------


async def fetch_dmami(
    group: str, *, headless: bool = True, chrome: Optional[str] = None
) -> List[WebEvent]:
    """
    Скрапит расписание для группы и возвращает список WebEvent.
    """
    group = (group or "").strip()
    if not group:
        raise ValueError("group must be non-empty")

    events: List[WebEvent] = []

    async with async_playwright() as pw:
        launch: Dict[str, Any] = {"headless": headless}
        if chrome:
            launch["executable_path"] = chrome
            if headless:
                launch["args"] = ["--headless=new"]
        else:
            # попытаться использовать системный Chrome, если есть
            launch["channel"] = "chrome"
            if headless:
                launch["args"] = ["--headless=new"]

        # надёжный запуск Chromium/Chrome
        try:
            browser = await pw.chromium.launch(**launch)
        except Exception:
            # запасной вариант — дефолтный Chromium
            browser = await pw.chromium.launch(headless=headless)

        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        try:
            # 1) главная и ввод группы
            await page.goto(DMAMI, wait_until="domcontentloaded", timeout=60_000)
            await _react_fill_and_trigger(page, "input.groups", group)

            # 2) ждём и кликаем найденную группу
            group_btn = f'.found-groups .group[id="{group}"]'
            await page.wait_for_selector(group_btn, timeout=7_000)
            await page.click(group_btn)

            # 3) ждём расписание и даём фронту дорисоваться
            await page.wait_for_selector(".schedule-day", timeout=30_000)
            await page.wait_for_timeout(800)
            await _auto_scroll(page, max_steps=22, step_px=1000, pause_ms=250)

            # 4) снимаем данные: все ссылки my.mts-link.ru
            raw_items: List[Dict[str, str]] = await page.evaluate(
                """() => {
                    return Array.from(document.querySelectorAll("a[href*='my.mts-link.ru']")).map(a => {
                      const pair  = a.closest('.pair') || a.closest('.schedule-lesson');
                      const timeEl  = pair?.querySelector('.time');
                      const time  = timeEl?.textContent?.trim() || '';
                      const titleEl = pair?.querySelector('.bold.small') || pair?.querySelector('.discipline-name');
                      const title = titleEl?.textContent?.trim() || '';
                      const dayRoot = a.closest('.schedule-day') || pair?.closest('.schedule-day');
                      const dayEl = dayRoot?.querySelector('.schedule-day__title');
                      const day   = dayEl?.textContent?.trim() || '';
                      return { day, time, title, href: a.href };
                    });
                }"""
            )

            for it in raw_items:
                start, end = _split_time(it.get("time", ""))
                title = (it.get("title") or "").strip()
                href = (it.get("href") or "").strip()
                day = (it.get("day") or "").strip()
                if href and title and start and end:
                    events.append(
                        WebEvent(day=day, start=start, end=end, title=title, href=href)
                    )
        finally:
            await context.close()
            await browser.close()

    return events


def to_yaml(events: List[WebEvent]) -> str:
    """
    Утилита: конвертировать список WebEvent в YAML-фрагмент для meetings.
    """
    import yaml  # локальный импорт, чтобы не тянуть в CLI, если не нужен

    arr: List[Dict[str, Any]] = []
    for e in events:
        arr.append(
            {
                "name": e.title,
                "url": e.href,
                "hint_time": f"{e.day} {e.start}-{e.end}",
            }
        )
    return yaml.safe_dump(arr, allow_unicode=True, sort_keys=False)


# ---------- CLI ----------

if __name__ == "__main__":
    import argparse
    import asyncio

    get_logs_dir().mkdir(parents=True, exist_ok=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, help="Номер группы, например: 231-363")
    ap.add_argument(
        "--headless", action="store_true", help="Запуск без окна браузера"
    )
    ap.add_argument(
        "--chrome",
        default=None,
        help="Путь к chrome.exe / chrome (если нужен строго системный)",
    )
    ap.add_argument("--out", default=None, help="Куда сохранить JSON")
    args = ap.parse_args()

    evs = asyncio.run(
        fetch_dmami(args.group, headless=args.headless, chrome=args.chrome)
    )
    print(f"Найдено ссылок: {len(evs)}")

    data_path = Path(args.out) if args.out else get_logs_dir() / "dmami_links.json"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(
        json.dumps([asdict(e) for e in evs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"JSON сохранён: {data_path}")

    print("\n--- YAML фрагмент ---")
    print(to_yaml(evs))
