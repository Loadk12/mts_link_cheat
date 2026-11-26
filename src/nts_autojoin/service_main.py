# src/nts_autojoin/service_main.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dateutil import tz

from .settings import get_logs_dir, load_config
from .logging_setup import setup_logger
from .power import keep_awake, on_ac_power
from .notifier import notify
from .scheduler import (
    _active_run_for_url,
    _keep_task,
    _meeting_active_on,
    catch_up_running_meetings,
    schedule_jobs,
    run_meeting,
    request_stop_active,
)
from .tg_commands import run_bot
from .link_updater import scrape_and_update
from .live import get_any_page


def _occurrences_today(
    cron_expr: str, tzinfo, limit_per_job: int = 8, from_start_of_day: bool = False
) -> List[datetime]:
    trigger = CronTrigger.from_crontab(cron_expr, timezone=tzinfo)
    now = datetime.now(tzinfo).replace(microsecond=0)
    eod = now.replace(hour=23, minute=59, second=59)
    last = None
    if from_start_of_day:
        cur = now.replace(hour=0, minute=0, second=0)
    else:
        cur = now - timedelta(seconds=1)
    out: List[datetime] = []
    for _ in range(limit_per_job):
        nxt = trigger.get_next_fire_time(last, cur)
        if not nxt or nxt.date() != now.date() or nxt > eod:
            break
        out.append(nxt)
        last = nxt
        cur = nxt + timedelta(seconds=1)
    return out


_MODE_SUFFIX = {
    "offline": " · очно",
    "online_manual": " · вручную",
}


def _today_digest_lines(cfg: Dict[str, Any], from_start_of_day: bool) -> List[str]:
    """
    Строки дайджеста на сегодня: только пары, у которых сегодня попадает
    в start_date/end_date, отсортированные по времени.
    """
    tzinfo = tz.gettz(cfg.get("timezone", "Europe/Moscow"))
    today = datetime.now(tzinfo).date()
    items: List[Tuple[datetime, str]] = []
    for m in cfg.get("meetings", []) or []:
        if not isinstance(m, dict):
            continue
        cron_expr = m.get("cron")
        if not cron_expr or not _meeting_active_on(m, today):
            continue
        name = m.get("name", "Без названия")
        dur = int(m.get("duration_minutes", 45))
        mode = m.get("meeting_mode") or ("online_auto" if m.get("url") else "")
        suffix = _MODE_SUFFIX.get(mode, "")
        if mode == "online_auto" and not m.get("url"):
            suffix = " · нет ссылки"
        try:
            occurrences = _occurrences_today(
                cron_expr, tzinfo, from_start_of_day=from_start_of_day
            )
        except Exception:
            continue
        for dt in occurrences:
            items.append((dt, f"• {dt.strftime('%H:%M')} — {name} ({dur} мин){suffix}"))
    items.sort(key=lambda x: x[0])
    return [line for _dt, line in items]


async def main():
    logger = setup_logger()
    cfg = load_config()

    if cfg.get("system", {}).get("require_ac_power", True):
        if not on_ac_power():
            logger.warning("Нет AC питания, не стартуем.")
            await notify(cfg, "⚠️ Нет AC питания, бот не запущен.")
            return
        else:
            logger.info("Работаем на AC питании.")
    else:
        logger.warning("Работаем без AC питания.")

    if cfg.get("system", {}).get("keep_awake", True):
        keep_awake(True)

    tzname = cfg.get("timezone", "Europe/Moscow")
    tzinfo = tz.gettz(tzname)

    def _reschedule(new_cfg: Dict[str, Any]) -> None:
        """
        Полностью пересобрать задачи планировщика: встречи, напоминания и
        служебные задачи (daily_summary). Затем догнать пары, которые уже идут.
        """
        nonlocal cfg
        scheduler.remove_all_jobs()
        schedule_jobs(scheduler, new_cfg, logger)
        cfg = new_cfg
        _schedule_service_jobs()
        started = catch_up_running_meetings(cfg, logger)
        if started:
            logger.info("catch-up started: %s", started)

    def _schedule_service_jobs() -> None:
        ncfg = cfg.get("notify") or {}
        if not ncfg.get("daily_summary"):
            return
        h = int(ncfg.get("daily_summary_hour", 7))
        m = int(ncfg.get("daily_summary_minute", 0))
        scheduler.add_job(
            _daily_summary,
            trigger=CronTrigger(hour=h, minute=m, timezone=tzinfo),
            id="daily_summary",
            name="daily_summary",
            coalesce=True,
            misfire_grace_time=600,
            max_instances=1,
            replace_existing=True,
        )

    async def _daily_summary():
        try:
            now = datetime.now(tzinfo)
            lines = [
                f"Доброе утро! План на сегодня ({now.strftime('%d.%m.%Y')}):"
            ]
            lines += _today_digest_lines(cfg, from_start_of_day=True) or ["• пар нет"]
            await notify(cfg, "\n".join(lines))
        except Exception as e:
            logger.warning(f"daily_summary error: {e}")

    async def _dmami_pull_job(notify_result: bool = False):
        try:
            updated, _changes = await scrape_and_update(cfg, logger)
            # перечитать конфиг и перепланировать
            _reschedule(load_config())
            report = _changes[0] if _changes else f"DMAMI sync завершён. Updated URLs: {updated}."
            if notify_result:
                await notify(cfg, report)
            return report
        except Exception as e:
            logger.error(f"DMAMI автопул ошибка: {e}")
            try:
                await notify(cfg, f"⛔ DMAMI автопул ошибка: {e}")
            except Exception:
                pass
            raise

    scheduler = AsyncIOScheduler(timezone=tzinfo)
    schedule_jobs(scheduler, cfg, logger)
    _schedule_service_jobs()

    scheduler.start()

    dmami_cfg = cfg.get("dmami") or {}
    generated_state = cfg.get("generated_schedule") or {}
    if dmami_cfg.get("auto_generate_schedule") and not generated_state.get("exists"):
        msg = (
            "DMAMI auto schedule включён, но generated schedule ещё не создан. "
            "Запустите /pull или кнопку «Обновить DMAMI»."
        )
        logger.warning(msg)
        try:
            await notify(cfg, msg)
        except Exception:
            pass
        if dmami_cfg.get("auto_generate_on_start", False):
            logger.info("dmami.auto_generate_on_start=true, starting DMAMI sync.")
            _keep_task(asyncio.create_task(_dmami_pull_job(notify_result=True)))

    # стартовый дайджест
    ncfg = cfg.get("notify") or {}
    if ncfg.get("on_start_summary"):
        try:
            lines = ["Бот стартанул. Оставшиеся встречи на сегодня:"]
            lines += _today_digest_lines(cfg, from_start_of_day=False) or ["• больше пар нет"]
            await notify(cfg, "\n".join(lines))
        except Exception as e:
            logger.warning(f"Стартовый дайджест не удался: {e}")

    # догоняем пару, если сервис стартовал посреди неё
    try:
        started = catch_up_running_meetings(cfg, logger)
        if started:
            logger.info("catch-up on start: %s", started)
    except Exception as e:
        logger.error(f"catch-up on start failed: {e}")

    async def reload_cb():
        try:
            _reschedule(load_config())
        except Exception as e:
            logger.error(f"reload error: {e}")
            raise

    async def screenshot_cb() -> str | None:
        """
        Скриншот активной страницы (Playwright). Возвращает путь к PNG или None.
        """
        try:
            page = get_any_page()
            if not page:
                return None
            tzinfo = tz.gettz(cfg.get("timezone", "Europe/Moscow"))
            ts = datetime.now(tzinfo).strftime("%Y%m%d-%H%M%S")
            path = get_logs_dir() / f"tg_shot_{ts}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return None

    async def dmami_pull_cb() -> str:
        """
        /pull — скрейп DMAMI, обновить ссылки, перепланировать.
        """
        return await _dmami_pull_job()

    async def connect_now_cb(url: str, hh: int, mm: int, dur_min: int) -> str:
        """
        /connect — адхок-подключение.

        Интерпретация аргументов:
        - /connect                         → берём ссылку из текущей пары и сидим duration_minutes.
        - /connect <url>                  → заходим сейчас, сидим dur_min минут (по умолчанию 90).
        - /connect <url> HH:MM            → заходим сейчас, выходим в HH:MM (сегодня, по таймзоне cfg).
        - /connect HH:MM                  → то же самое, но ссылку берём из расписания.

        Если HH:MM уже в прошлом — используем dur_min минут от текущего момента
        (но не меньше 5 минут).
        """
        nonlocal cfg
        now = datetime.now(tzinfo)

        # трактуем HH:MM как целевое время выхода, если оно ещё не наступило сегодня
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target > now:
            seconds_left = (target - now).total_seconds()
            dur = max(1, int((seconds_left + 59) // 60))
            end = target
        else:
            # время уже прошло → воспринимаем dur_min как «сидеть столько минут от сейчас»
            dur = max(int(dur_min), 5)
            end = now + timedelta(minutes=dur)

        # подберём шаги входа: либо default_join, либо join первой встречи из расписания
        default_join = cfg.get("default_join")
        if default_join:
            join_cfg = default_join
        else:
            meetings = cfg.get("meetings") or []
            if meetings and isinstance(meetings[0], dict):
                join_cfg = meetings[0].get("join", {}) or {}
            else:
                join_cfg = {}

        meeting = {
            "name": f"Ad-hoc ({hh:02d}:{mm:02d}, {dur} мин)",
            "url": url,
            "duration_minutes": dur,
            "deadline_at": end,
            "join": join_cfg,
        }

        chromium_cfg = cfg.get("chromium", {}) or {}
        hc_cfg = cfg.get("healthcheck", {}) or {}

        busy_with = _active_run_for_url(url)
        if busy_with:
            return (
                f"ℹ️ Уже сижу в этой встрече: [{busy_with}].\n"
                f"Для перезахода сначала /disconnect."
            )

        # запускаем как независимую задачу, чтобы не блокировать бота
        _keep_task(
            asyncio.create_task(
                run_meeting(meeting, tzname, chromium_cfg, hc_cfg, cfg, logger)
            )
        )
        return (
            f"⚡ Подключаюсь сейчас.\n"
            f"Выйду в {end.strftime('%H:%M')} (остаток ~{dur} мин)."
        )

    async def disconnect_cb() -> str:
        names = request_stop_active()
        if not names:
            return "ℹ️ Сейчас нет активных подключений."
        stopped = "\n".join(f"• {n}" for n in names)
        return f"⏹️ Останавливаю встречи:\n{stopped}"

    # стартуем Telegram-бота
    bot_task = asyncio.create_task(
        run_bot(
            cfg,
            reload_cb,
            screenshot_cb,
            dmami_pull_cb,
            connect_now_cb,
            disconnect_cb,
            logger,
        )
    )

    try:
        await asyncio.Event().wait()  # просто держим main живым
    finally:
        bot_task.cancel()
        try:
            await bot_task
        except Exception:
            pass
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
