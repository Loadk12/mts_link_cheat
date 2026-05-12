# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import Page, async_playwright

try:
    from .settings import get_logs_dir
except ImportError:
    from nts_autojoin.settings import get_logs_dir

DMAMI = "https://rasp.dmami.ru"
TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})")


@dataclass
class WebEvent:
    day: str
    start: str
    end: str
    title: str
    href: str = ""
    lesson_type: str = ""
    date: str = ""
    teacher: str = ""
    room: str = ""
    raw_text: str = ""
    url_kind: str = "none"
    date_range_raw: str = ""
    start_date: str = ""
    end_date: str = ""
    outer_html: str = ""


async def _react_fill_and_trigger(page: Page, selector: str, value: str) -> None:
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
    for _ in range(max_steps):
        await page.mouse.wheel(0, step_px)
        await page.wait_for_timeout(pause_ms)
    for _ in range(3):
        await page.mouse.wheel(0, -step_px)
        await page.wait_for_timeout(pause_ms)


def _split_time(value: str) -> tuple[str, str]:
    match = TIME_RE.search((value or "").strip())
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def _url_kind(url: str) -> str:
    if not url:
        return "none"
    return "mts_link" if "my.mts-link.ru" in url.lower() else "external_link"


async def _save_debug_artifacts(page: Page, prefix: str, screenshot: bool) -> Dict[str, str]:
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    html_path = logs_dir / f"{prefix}.html"
    png_path = logs_dir / f"{prefix}.png"
    html_path.write_text(await page.content(), encoding="utf-8", errors="ignore")
    out = {"html": str(html_path)}
    if screenshot:
        await page.screenshot(path=str(png_path), full_page=True)
        out["png"] = str(png_path)
    return out


async def fetch_dmami(
    group: str,
    *,
    headless: bool = True,
    chrome: Optional[str] = None,
    debug: bool = False,
    debug_prefix: Optional[str] = None,
) -> List[WebEvent]:
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
            launch["channel"] = "chrome"
            if headless:
                launch["args"] = ["--headless=new"]

        try:
            browser = await pw.chromium.launch(**launch)
        except Exception:
            browser = await pw.chromium.launch(headless=headless)

        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        try:
            await page.goto(DMAMI, wait_until="domcontentloaded", timeout=60_000)
            await _react_fill_and_trigger(page, "input.groups", group)
            group_btn = f'.found-groups .group[id="{group}"]'
            await page.wait_for_selector(group_btn, timeout=7_000)
            await page.click(group_btn)

            await page.wait_for_selector(".schedule-day", timeout=30_000)
            await page.wait_for_timeout(1000)
            await _auto_scroll(page, max_steps=24, step_px=1000, pause_ms=250)

            if debug_prefix:
                await _save_debug_artifacts(page, debug_prefix, screenshot=debug)

            raw_items: List[Dict[str, str]] = await page.evaluate(
                """() => {
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const timeRe = /(\\d{1,2}:\\d{2})\\s*[-–—]\\s*(\\d{1,2}:\\d{2})/;
                    const dateRe = /\\d{1,2}\\s*[А-Яа-яЁё]{3,}\\s*[-–—]\\s*\\d{1,2}\\s*[А-Яа-яЁё]{3,}(?:\\s*\\d{4})?/;
                    const titleSelectors = [
                      '.bold.small', '.discipline-name', '.lesson-title',
                      '.subject', '.title', '[class*="discipline"]'
                    ];
                    const typeSelectors = ['.lesson-type', '.type', '.kind', '[class*="type"]'];
                    const teacherSelectors = ['.teacher', '.lecturer', '[class*="teacher"]'];
                    const roomSelectors = ['.room', '.auditory', '.auditorium', '[class*="auditor"]'];
                    const lessonSelectors = [
                      '.schedule-lesson', '.lesson', '[class*="lesson"]'
                    ];
                    const pairSelectors = ['.pair', '.schedule-pair', '[class*="pair"]'];

                    const pick = (root, selectors) => {
                      for (const sel of selectors) {
                        const el = root?.querySelector?.(sel);
                        const text = norm(el?.textContent || '');
                        if (text) return text;
                      }
                      return '';
                    };

                    const dayTitle = (root) => {
                      const dayRoot = root.closest('.schedule-day') || root.closest('[class*="schedule-day"]');
                      const el = dayRoot?.querySelector('.schedule-day__title, [class*="day__title"], [class*="day-title"]');
                      return norm(el?.textContent || dayRoot?.querySelector('h2,h3,h4')?.textContent || '');
                    };

                    const cards = new Set();
                    for (const sel of lessonSelectors) {
                      document.querySelectorAll(sel).forEach((el) => {
                        const pair = el.closest(pairSelectors.join(','));
                        const pairText = norm(pair?.textContent || '');
                        if (timeRe.test(pairText)) cards.add(el);
                      });
                    }
                    for (const sel of pairSelectors) {
                      document.querySelectorAll(sel).forEach((el) => {
                        if (!el.querySelector(lessonSelectors.join(',')) && timeRe.test(norm(el.textContent))) {
                          cards.add(el);
                        }
                      });
                    }
                    document.querySelectorAll('.schedule-day, [class*="schedule-day"]').forEach((day) => {
                      day.querySelectorAll('*').forEach((el) => {
                        const text = norm(el.textContent);
                        if (!timeRe.test(text)) return;
                        const card = el.closest(lessonSelectors.join(',')) || el.closest(pairSelectors.join(',')) || el;
                        cards.add(card);
                      });
                    });

                    return Array.from(cards).map((card) => {
                      const text = norm(card.textContent);
                      const pair = card.closest(pairSelectors.join(','));
                      const timeText = norm(pair?.querySelector('.time, [class*="time"]')?.textContent || card.querySelector('.time, [class*="time"]')?.textContent || text);
                      const m = timeText.match(timeRe) || text.match(timeRe);
                      const link = Array.from(card.querySelectorAll('a[href]')).find((a) => /^https?:/i.test(a.href));
                      const href = link?.href || '';
                      const dateMatch = text.match(dateRe);
                      let title = pick(card, titleSelectors);
                      if (!title) {
                        title = text
                          .replace(timeRe, ' ')
                          .replace(dateRe, ' ')
                          .split(/(?:преподаватель|аудитория|каб|онлайн|http)/i)[0]
                          .trim();
                      }
                      return {
                        day: dayTitle(card),
                        time: m ? `${m[1]}-${m[2]}` : '',
                        title,
                        href,
                        lesson_type: pick(card, typeSelectors),
                        teacher: pick(card, teacherSelectors),
                        room: pick(card, roomSelectors),
                        date_range_raw: dateMatch ? dateMatch[0] : '',
                        raw_text: text,
                        outer_html: card.outerHTML || ''
                      };
                    });
                }"""
            )

            seen: set[tuple[str, str, str, str]] = set()
            for item in raw_items:
                start, end = _split_time(item.get("time", ""))
                title = (item.get("title") or "").strip()
                day = (item.get("day") or "").strip()
                href = (item.get("href") or "").strip()
                date_range_raw = (item.get("date_range_raw") or "").strip()
                key = (day, start, end, title, href, date_range_raw)
                if not day or not title or not start or not end or key in seen:
                    continue
                seen.add(key)
                events.append(
                    WebEvent(
                        day=day,
                        start=start,
                        end=end,
                        title=title,
                        href=href,
                        lesson_type=(item.get("lesson_type") or "").strip(),
                        teacher=(item.get("teacher") or "").strip(),
                        room=(item.get("room") or "").strip(),
                        raw_text=(item.get("raw_text") or "").strip(),
                        url_kind=_url_kind(href),
                        date_range_raw=date_range_raw,
                        outer_html=(item.get("outer_html") or "").strip(),
                    )
                )
        finally:
            await context.close()
            await browser.close()

    return events


def to_yaml(events: List[WebEvent]) -> str:
    import yaml

    return yaml.safe_dump(
        [asdict(event) for event in events], allow_unicode=True, sort_keys=False
    )


if __name__ == "__main__":
    import argparse
    import asyncio
    import sys
    from datetime import datetime

    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--chrome", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    headless = args.headless and not args.no_headless
    if args.no_headless:
        headless = False
    prefix = f"dmami_page_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    evs = asyncio.run(
        fetch_dmami(
            args.group,
            headless=headless,
            chrome=args.chrome,
            debug=args.debug,
            debug_prefix=prefix if args.debug else None,
        )
    )
    print(f"Found lesson cards: {len(evs)}")

    out_path = Path(args.out) if args.out else get_logs_dir() / "dmami_cards.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps([asdict(e) for e in evs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"JSON saved: {out_path}")
    sys.stdout.buffer.write(to_yaml(evs).encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")
