# src/nts_autojoin/scheduler.py
import asyncio
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict

from dateutil import tz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .browser import create_browser
from .join_flow import perform_join
from .healthcheck import page_is_healthy
from .live import set_page, clear_page
from .notifier import notify, send_photo


def _tz(tzname: str):
    return tz.gettz(tzname or "Europe/Moscow")


def _sanitize(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name or "meeting")


def _now(tzname: str) -> datetime:
    return datetime.now(_tz(tzname))


def _ensure_logs_dir():
    Path("logs").mkdir(parents=True, exist_ok=True)


async def _save_artifacts(page, prefix: str, tzname: str, logger) -> str:
    """
    Сохранить скрин и HTML текущей страницы. Возвращает базовый путь без расширения.
    """
    _ensure_logs_dir()
    ts = _now(tzname).strftime("%Y%m%d-%H%M%S")
    base = f"logs/{prefix}_{ts}"
    try:
        await page.screenshot(path=f"{base}.png", full_page=True)
    except Exception as e:
        logger.warning(f"artifact screenshot failed: {e}")
    try:
        html = await page.content()
        Path(f"{base}.html").write_text(html, encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"artifact html save failed: {e}")
    return base


def _parse_cfg_date(value):
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


async def run_meeting(
    meeting: Dict,
    tzname: str,
    chromium_cfg: dict,
    hc_cfg: dict,
    global_cfg: dict,
    logger,
):
    """
    Одна встреча: открываем Chrome, проходим join, проверяем здоровье страницы,
    при сбоях сохраняем скрины/HTML и шлём алерты.
    """
    name = meeting.get("name", "Безымянка")
    url = meeting["url"]
    duration_min = int(meeting.get("duration_minutes", 45))
    join_cfg = meeting.get("join", {}) or {}

    # Опциональный фильтр по диапазону дат (start_date/end_date).
    # Если сегодня вне диапазона — мягко выходим, ничего не делаем.
    tzinfo = _tz(tzname)
    today = datetime.now(tzinfo).date()

    start_date = _parse_cfg_date(
        meeting.get("start_date")
        or meeting.get("date_from")
        or meeting.get("from_date")
    )
    end_date = _parse_cfg_date(
        meeting.get("end_date")
        or meeting.get("date_to")
        or meeting.get("to_date")
    )

    if (start_date and today < start_date) or (end_date and today > end_date):
        logger.info(
            f"[{name}] сегодня ({today}) вне диапазона дат "
            f"({start_date or '-'}–{end_date or '-'}) — пропускаю запуск."
        )
        return

    fail_threshold = int(hc_cfg.get("fail_threshold", 2))
    every_minutes = int(hc_cfg.get("every_minutes", 5))

    pw = browser = context = page = None
    prefix = _sanitize(name)

    await notify(global_cfg, f"▶️ [{name}] старт.")

    try:
        # Старт браузера
        pw, browser = await create_browser(chromium_cfg)
        context = await browser.new_context()
        page = await context.new_page()
        set_page(name, page)

        # Переходим на ссылку и выполняем join-скрипт
        await page.goto(url, wait_until="domcontentloaded")
        if join_cfg:
            await perform_join(page, join_cfg)

        deadline = _now(tzname) + timedelta(minutes=duration_min)
        consecutive_failures = 0

        while _now(tzname) < deadline:
            try:
                ok = await page_is_healthy(page, hc_cfg)
            except Exception as e:
                logger.warning(f"[{name}] healthcheck error: {e}")
                ok = False

            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                logger.warning(
                    f"[{name}] healthcheck FAIL "
                    f"({consecutive_failures}/{fail_threshold})"
                )
                base = await _save_artifacts(page, prefix, tzname, logger)
                try:
                    await send_photo(
                        global_cfg,
                        f"{base}.png",
                        caption=f"⛔ [{name}] healthcheck fail",
                    )
                except Exception:
                    pass
                if consecutive_failures >= fail_threshold:
                    await notify(
                        global_cfg,
                        f"⛔ [{name}] здоровье страницы упало, выхожу раньше срока.",
                    )
                    break

            await asyncio.sleep(max(30, every_minutes * 60))

        if _now(tzname) >= deadline:
            await notify(global_cfg, f"⏹️ [{name}] время вышло, выхожу.")
    except Exception as e:
        logger.error(f"[{name}] meeting error: {e}")
        if page is not None:
            base = await _save_artifacts(page, prefix, tzname, logger)
            try:
                await send_photo(
                    global_cfg,
                    f"{base}.png",
                    caption=f"⛔ [{name}] ошибка встречи",
                )
            except Exception:
                pass
        try:
            await notify(global_cfg, f"⛔ [{name}] ошибка: {e}")
        except Exception:
            pass
    finally:
        try:
            clear_page(name)
        except Exception:
            pass
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass


def schedule_jobs(scheduler: AsyncIOScheduler, cfg: dict, logger):
    """
    Вешаем cron-задачи на все встречи из конфигурации.
    Фильтр по датам в run_meeting всё равно сработает, так что здесь
    просто раскладываем cron по дням недели/времени.
    """
    tzname = cfg.get("timezone", "Europe/Moscow")
    chromium_cfg = cfg.get("chromium", {}) or {}
    hc_cfg = cfg.get("healthcheck", {}) or {}

    tzinfo = _tz(tzname)

    for m in cfg.get("meetings", []) or []:
        cron_expr = m.get("cron")
        if not cron_expr:
            continue

        trigger = CronTrigger.from_crontab(cron_expr, timezone=tzinfo)
        scheduler.add_job(
            run_meeting,
            trigger=trigger,
            args=[m, tzname, chromium_cfg, hc_cfg, cfg, logger],
            name=m.get("name", m.get("url", "(no url)")),
            coalesce=True,
            misfire_grace_time=600,
            max_instances=1,
            replace_existing=True,
        )
        logger.info(
            f"Запланировано: {m.get('name','(no name)')} @ {cron_expr} [{tzname}]"
        )
