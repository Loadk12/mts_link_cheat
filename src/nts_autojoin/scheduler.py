# src/nts_autojoin/scheduler.py
import asyncio
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from dateutil import tz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .browser import create_browser
from .join_flow import perform_join, ensure_media_disabled
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


# Активные встречи: name -> (stop_event, started_at)
_active_runs: dict[str, Tuple[asyncio.Event, datetime]] = {}


def active_meetings() -> List[str]:
    return list(_active_runs.keys())


def request_stop_active() -> List[str]:
    """Запросить остановку всех текущих встреч. Возвращает их имена."""

    names = []
    for name, (ev, _started) in list(_active_runs.items()):
        if not ev.is_set():
            ev.set()
            names.append(name)
    return names


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


async def _send_success_screenshot(page, prefix: str, tzname: str, cfg: dict, name: str, logger):
    """Отправить скриншот спустя 10 секунд после успешного входа."""

    try:
        await page.wait_for_timeout(10_000)
        base = await _save_artifacts(page, f"{prefix}_joined", tzname, logger)
        try:
            await send_photo(
                cfg,
                f"{base}.png",
                caption=f"✅ [{name}] скрин после входа",
            )
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"[{name}] не удалось отправить скриншот после входа: {e}")


async def _run_meeting_attempt(
    name: str,
    url: str,
    join_cfg: Dict,
    chromium_cfg: dict,
    hc_cfg: dict,
    global_cfg: dict,
    logger,
    stop_event: asyncio.Event,
    deadline: datetime,
    fail_threshold: int,
    every_minutes: int,
    prefix: str,
    attempt: int,
    total_attempts: int,
    tzname: str,
):
    pw = browser = context = page = None
    success_shot_task = None

    try:
        pw, browser = await create_browser(chromium_cfg)
        context = await browser.new_context()
        page = await context.new_page()
        set_page(name, page)

        await page.goto(url, wait_until="domcontentloaded")
        already_joined = False
        try:
            already_joined = await page_is_healthy(page, hc_cfg)
            if already_joined:
                logger.info(
                    f"[{name}] похоже, уже в встрече — пропускаю шаги join."
                )
        except Exception as e:
            logger.warning(f"[{name}] healthcheck перед join не сработал: {e}")

        if join_cfg and not already_joined:
            await perform_join(page, join_cfg)
        try:
            await ensure_media_disabled(page)
        except Exception as e:
            logger.warning(f"[{name}] не удалось гарантировать выключение медиа: {e}")

        success_shot_task = asyncio.create_task(
            _send_success_screenshot(page, prefix, tzname, global_cfg, name, logger)
        )

        consecutive_failures = 0
        ever_healthy = False

        while True:
            if stop_event.is_set():
                await notify(global_cfg, f"⏹️ [{name}] остановлен вручную.")
                return True

            now = _now(tzname)
            if now >= deadline:
                await notify(global_cfg, f"⏹️ [{name}] время вышло, выхожу.")
                return True

            try:
                ok = await page_is_healthy(page, hc_cfg)
            except Exception as e:
                logger.warning(f"[{name}] healthcheck error: {e}")
                ok = False

            if ok:
                ever_healthy = True
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
                        caption=f"⛔ [{name}] healthcheck fail (попытка {attempt}/{total_attempts})",
                    )
                except Exception:
                    pass
                if consecutive_failures >= fail_threshold:
                    await notify(
                        global_cfg,
                        f"⛔ [{name}] здоровье страницы упало, перезапускаю попытку.",
                    )
                    return False

            sleep_for = max(30, every_minutes * 60)
            remaining = max(0, (deadline - _now(tzname)).total_seconds())
            timeout = min(sleep_for, remaining) if remaining else sleep_for
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    except Exception as e:
        logger.error(f"[{name}] meeting error: {e}")
        if page is not None:
            base = await _save_artifacts(page, prefix, tzname, logger)
            try:
                await send_photo(
                    global_cfg,
                    f"{base}.png",
                    caption=f"⛔ [{name}] ошибка встречи (попытка {attempt}/{total_attempts})",
                )
            except Exception:
                pass
        try:
            await notify(global_cfg, f"⛔ [{name}] ошибка: {e}")
        except Exception:
            pass
        return False
    finally:
        try:
            if success_shot_task and not success_shot_task.done():
                success_shot_task.cancel()
        except Exception:
            pass
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

    return True if ever_healthy else False


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
    при сбоях сохраняем скрины/HTML и шлём алерты. Добавлены автоскрины после
    входа и повторные попытки при неудаче.
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

    prefix = _sanitize(name)
    deadline = _now(tzname) + timedelta(minutes=duration_min)

    await notify(global_cfg, f"▶️ [{name}] старт.")

    stop_event = asyncio.Event()
    _active_runs[name] = (stop_event, _now(tzname))

    retry_delays = [0, 60, 120, 300]
    total_attempts = len(retry_delays)
    success = False

    try:
        for attempt, delay in enumerate(retry_delays, start=1):
            if stop_event.is_set():
                break

            if delay:
                try:
                    await notify(
                        global_cfg,
                        f"🔁 [{name}] повторная попытка через {int(delay/60)} мин "
                        f"({attempt}/{total_attempts}).",
                    )
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break

            attempt_prefix = f"{prefix}_try{attempt}"
            attempt_success = await _run_meeting_attempt(
                name,
                url,
                join_cfg,
                chromium_cfg,
                hc_cfg,
                global_cfg,
                logger,
                stop_event,
                deadline,
                fail_threshold,
                every_minutes,
                attempt_prefix,
                attempt,
                total_attempts,
                tzname,
            )
            if attempt_success:
                success = True
                break

        if not success and not stop_event.is_set():
            try:
                await notify(global_cfg, f"⛔ [{name}] все попытки подключения исчерпаны.")
            except Exception:
                pass
    finally:
        try:
            _active_runs.pop(name, None)
        except Exception:
            pass
        try:
            clear_page(name)
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
