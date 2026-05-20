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
from .healthcheck import is_joined_to_meeting, page_is_healthy
from .live import set_page, clear_page
from .notifier import notify, send_photo
from .settings import get_logs_dir


def _tz(tzname: str):
    return tz.gettz(tzname or "Europe/Moscow")


def _sanitize(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name or "meeting")


def _now(tzname: str) -> datetime:
    return datetime.now(_tz(tzname))


def _ensure_logs_dir():
    get_logs_dir().mkdir(parents=True, exist_ok=True)


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
    base = get_logs_dir() / f"{prefix}_{ts}"
    try:
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception as e:
        logger.warning(f"artifact screenshot failed: {e}")
    try:
        html = await page.content()
        base.with_suffix(".html").write_text(html, encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"artifact html save failed: {e}")
    try:
        controls = await page.locator("button, [role='button'], input, textarea").evaluate_all(
            """els => els.slice(0, 80).map((el, i) => ({
                index: i,
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type') || '',
                text: (el.innerText || el.textContent || '').trim(),
                disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                cls: el.getAttribute('class') || '',
                aria: el.getAttribute('aria-label') || '',
                title: el.getAttribute('title') || '',
                testid: el.getAttribute('data-testid') || '',
                placeholder: el.getAttribute('placeholder') || ''
            }))"""
        )
        lines = [f"url: {page.url}", "", "buttons/inputs:"]
        for item in controls:
            lines.append(
                f"- #{item.get('index')} tag={item.get('tag')!r} type={item.get('type')!r} "
                f"text={item.get('text')!r} "
                f"disabled={item.get('disabled')!r} class={item.get('cls')!r} "
                f"aria={item.get('aria')!r} title={item.get('title')!r} "
                f"data-testid={item.get('testid')!r} placeholder={item.get('placeholder')!r}"
            )
        base.with_suffix(".txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        logger.warning(f"artifact diagnostics save failed: {e}")
    return str(base)


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


async def _hold_meeting_until_deadline(
    page,
    name: str,
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
) -> bool:
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
                if await is_joined_to_meeting(page):
                    logger.warning(f"[{name}] healthcheck failed, but in-meeting UI is visible; continuing.")
                    consecutive_failures = 0
                else:
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

    return True if ever_healthy else False


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
        context_kwargs = {}
        if chromium_cfg.get("visible_debug"):
            context_kwargs["viewport"] = None
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        navigation_timeout_ms = int(chromium_cfg.get("navigation_timeout_ms", 120000) or 120000)
        page.set_default_navigation_timeout(navigation_timeout_ms)
        set_page(name, page)

        join_timeouts = global_cfg.get("join_timeouts", {}) or {}
        page_goto_ms = int(join_timeouts.get("page_goto_ms", navigation_timeout_ms))

        await page.goto(url, wait_until="domcontentloaded", timeout=page_goto_ms)
        logger.info(f"[{name}] opening landing page: {page.url}")
        already_joined = False
        try:
            already_joined = await is_joined_to_meeting(page)
            if already_joined:
                logger.info(f"[{name}] detected in-meeting UI before join; skipping join steps.")
        except Exception as e:
            logger.warning(f"[{name}] pre-join meeting detection failed: {e}")

        if join_cfg and not already_joined:
            try:
                joined = await perform_join(page, join_cfg, logger=logger, timeouts=join_timeouts)
                if joined:
                    logger.info(f"[{name}] join success.")
                elif await is_joined_to_meeting(page):
                    logger.warning(f"[{name}] join flow returned false, but meeting UI is visible; continuing.")
                else:
                    raise TimeoutError("join flow finished without detecting meeting UI")
            except Exception as e:
                if await is_joined_to_meeting(page):
                    logger.warning(f"[{name}] join timeout/error but already inside meeting; continuing: {e}")
                else:
                    logger.warning(f"[{name}] join failed: {e}")
                    raise
        try:
            await ensure_media_disabled(page)
        except Exception as e:
            logger.warning(f"[{name}] не удалось гарантировать выключение медиа: {e}")

        success_shot_task = asyncio.create_task(
            _send_success_screenshot(page, prefix, tzname, global_cfg, name, logger)
        )

        return await _hold_meeting_until_deadline(
            page,
            name,
            hc_cfg,
            global_cfg,
            logger,
            stop_event,
            deadline,
            fail_threshold,
            every_minutes,
            prefix,
            attempt,
            total_attempts,
            tzname,
        )

    except Exception as e:
        logger.error(f"[{name}] meeting error: {e}")
        try:
            if page is not None and await is_joined_to_meeting(page):
                logger.warning(f"[{name}] join failed with timeout/error, but meeting UI is visible; continuing.")
                return await _hold_meeting_until_deadline(
                    page,
                    name,
                    hc_cfg,
                    global_cfg,
                    logger,
                    stop_event,
                    deadline,
                    fail_threshold,
                    every_minutes,
                    prefix,
                    attempt,
                    total_attempts,
                    tzname,
                )
        except Exception:
            pass
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

    return False


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


def _meeting_active_on(meeting: Dict, day) -> bool:
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
    if start_date and day < start_date:
        return False
    if end_date and day > end_date:
        return False
    return True


def _can_auto_join(meeting: Dict) -> bool:
    return bool(meeting.get("url")) and meeting.get("meeting_mode", "online_auto") == "online_auto"


def _connect_template(meeting: Dict) -> str:
    dmami = meeting.get("dmami") or {}
    return f"/connect https://... {dmami.get('end') or 'HH:MM'}"


async def send_meeting_reminder(
    meeting: Dict,
    tzname: str,
    global_cfg: dict,
    logger,
    minutes_before: int,
):
    name = meeting.get("name", "Без названия")
    tzinfo = _tz(tzname)
    today = datetime.now(tzinfo).date()
    if not _meeting_active_on(meeting, today):
        logger.info("[%s] reminder skipped outside date range", name)
        return

    dmami = meeting.get("dmami") or {}
    start = dmami.get("start") or "?"
    end = dmami.get("end") or "?"
    if minutes_before > 0:
        prefix = f"Через {minutes_before} минут начнётся: {name}, {start}-{end}."
    else:
        prefix = f"Пара началась: {name}, {start}-{end}."

    if meeting.get("meeting_mode") == "offline":
        tail = f"Похоже, это очная пара. Аудитория: {dmami.get('room') or '-'}."
    elif meeting.get("url"):
        tail = "Автоподключение запланировано." if _can_auto_join(meeting) else "Есть ссылка, можно подключиться вручную."
    else:
        tail = f"Ссылки нет. Если пара онлайн, пришли ссылку командой: {_connect_template(meeting)}"

    await notify(global_cfg, f"{prefix}\n{tail}")


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
    url = meeting.get("url")
    if not url or not _can_auto_join(meeting):
        logger.info(
            "[%s] auto-join skipped: mode=%s url=%s",
            name,
            meeting.get("meeting_mode"),
            bool(url),
        )
        return
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
            send_meeting_reminder,
            trigger=trigger,
            args=[m, tzname, cfg, logger, 0],
            name=f"reminder_start:{m.get('name', '(no name)')}",
            coalesce=True,
            misfire_grace_time=600,
            max_instances=1,
            replace_existing=True,
        )
        before_expr = _shift_cron_minutes(cron_expr, -5)
        if before_expr:
            scheduler.add_job(
                send_meeting_reminder,
                trigger=CronTrigger.from_crontab(before_expr, timezone=tzinfo),
                args=[m, tzname, cfg, logger, 5],
                name=f"reminder_5m:{m.get('name', '(no name)')}",
                coalesce=True,
                misfire_grace_time=600,
                max_instances=1,
                replace_existing=True,
            )
        if _can_auto_join(m):
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


def _shift_cron_minutes(cron_expr: str, delta_minutes: int) -> str | None:
    parts = (cron_expr or "").split()
    if len(parts) != 5:
        return None
    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except Exception:
        return None
    day = parts[4].lower()
    weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    total = hour * 60 + minute + delta_minutes
    if total < 0:
        total += 24 * 60
        if day in weekdays:
            day = weekdays[(weekdays.index(day) - 1) % 7]
    elif total >= 24 * 60:
        total -= 24 * 60
        if day in weekdays:
            day = weekdays[(weekdays.index(day) + 1) % 7]
    return f"{total % 60} {total // 60} {parts[2]} {parts[3]} {day}"
