# src/nts_autojoin/tg_commands.py
import asyncio, os, re
from pathlib import Path
from typing import Callable, Awaitable, List, Optional, Tuple

import aiohttp
import yaml
from dateutil import tz
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta

from .notifier import notify, send_document, send_photo
from .healthcheck import page_is_healthy
from .live import get_any_page
from .settings import get_logs_dir, load_config
from .cron_helpers import next_fire_time, prev_fire_time
BASE_DIR = Path(__file__).resolve().parents[2]
OFFSET_FILE = BASE_DIR / "logs" / "tg_offset.state"
STALE_SEC = 300  # игнор опасных команд старше 5 минут


def _tz(cfg):
    return tz.gettz(cfg.get("timezone", "Europe/Moscow"))


def _parse_cfg_date(value):
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            # datetime.strptime вернёт datetime; берём .date()
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _meeting_active_on(m: dict, day) -> bool:
    """Проверяем, попадает ли day (date) в диапазон start_date/end_date, если он задан в YAML."""
    start_date = _parse_cfg_date(
        m.get("start_date") or m.get("date_from") or m.get("from_date")
    )
    end_date = _parse_cfg_date(
        m.get("end_date") or m.get("date_to") or m.get("to_date")
    )
    if start_date and day < start_date:
        return False
    if end_date and day > end_date:
        return False
    return True


def _meeting_mode_label(m: dict) -> str:
    mode = m.get("meeting_mode") or "unknown"
    return {
        "online_auto": "Auto",
        "online_manual": "Manual",
        "offline": "Offline",
        "unknown": "Unknown",
    }.get(mode, mode)


def _date_range_label(m: dict) -> str:
    start = m.get("start_date")
    end = m.get("end_date")
    if start and end:
        return f"{start}..{end}"
    return "без дат"


def _connect_hint(m: dict) -> str:
    dmami = m.get("dmami") or {}
    return f"/connect https://... {dmami.get('end') or 'HH:MM'}"


def _occurrences_today(cron_expr: str, tzinfo) -> List[datetime]:
    trig = CronTrigger.from_crontab(cron_expr, timezone=tzinfo)
    now = datetime.now(tzinfo)
    occ = []
    prev, cur = None, now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(20):
        nxt = trig.get_next_fire_time(prev, cur)
        if not nxt or nxt.date() != now.date():
            break
        occ.append(nxt)
        prev, cur = nxt, nxt
    return occ


def _next_occurrence(cron_expr: str, tzinfo):
    """Вернуть ближайшее срабатывание cron после текущего момента."""

    now = datetime.now(tzinfo)
    return next_fire_time(cron_expr, tzinfo, now)


def _load_cfg() -> dict:
    # Используем общий загрузчик, чтобы путь работал и из под nssm/systemd,
    # где рабочая директория может отличаться от корня репозитория.
    return load_config()


def _tg(cfg):
    """Возвращает настройки Telegram: токен, чат и список разрешённых ID."""

    n = cfg.get("notify") or {}
    return (
        n.get("telegram_bot_token"),
        n.get("telegram_chat_id"),
        n.get("allowed_user_ids") or [],
    )


async def _send(cfg, text: str, chat_id: Optional[int] = None, reply_markup: Optional[dict] = None):
    token, default_chat, *_ = _tg(cfg)
    chat = chat_id or default_chat
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        data["reply_markup"] = reply_markup
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=data, timeout=60) as r:
                await r.text()
    except Exception:
        pass


async def _edit_or_send(
    cfg,
    chat_id: int,
    message_id: Optional[int],
    text: str,
    reply_markup: Optional[dict] = None,
):
    token, *_ = _tg(cfg)
    if not token or not chat_id or not message_id:
        await _send(cfg, text, chat_id, reply_markup)
        return
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=data, timeout=60) as r:
                resp = await r.json()
        if not resp.get("ok"):
            await _send(cfg, text, chat_id, reply_markup)
    except Exception:
        await _send(cfg, text, chat_id, reply_markup)


async def _answer_callback(cfg, callback_id: str, text: str = ""):
    token, *_ = _tg(cfg)
    if not token or not callback_id:
        return
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=data, timeout=30) as r:
                await r.text()
    except Exception:
        pass


def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def _main_keyboard() -> dict:
    return _kb(
        [
            [("🔌 Подключиться сейчас", "act:connect")],
            [("📅 Сегодня", "menu:today"), ("🔄 Обновить DMAMI", "act:pull")],
            [("📷 Скрин", "act:shot"), ("❤️ Healthcheck", "act:health")],
            [("🧾 Логи", "menu:logs")],
        ]
    )


def _back_keyboard() -> dict:
    return _kb([[("🔙 Назад", "menu:home"), ("🔄 Обновить", "menu:today")]])


def _pull_confirm_keyboard() -> dict:
    return _kb([[("Да, обновить", "confirm:pull"), ("Отмена", "cancel")]])


def _logs_keyboard() -> dict:
    return _kb(
        [
            [("service_stderr.log", "log:service_stderr"), ("service_stdout.log", "log:service_stdout")],
            [("dmami_changes", "log:dmami_changes"), ("dmami_generated", "log:dmami_generated")],
            [("🔙 Назад", "menu:home")],
        ]
    )


def _read_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _write_offset(value: int):
    try:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(str(value), encoding="utf-8")
    except Exception:
        pass


def _today_summary(cfg: dict) -> str:
    tzinfo = _tz(cfg)
    now = datetime.now(tzinfo)
    items = []
    for m in cfg.get("meetings", []) or []:
        if not _meeting_active_on(m, now.date()):
            continue
        cron = m.get("cron")
        if not cron:
            continue
        for dt in _occurrences_today(cron, tzinfo):
            items.append((dt, m))
    if not items:
        # подскажем, когда стартует ближайшая встреча из расписания —
        # это поможет заметить ошибочные даты (например, 2025 вместо 2024).
        closest = []
        for m in cfg.get("meetings", []) or []:
            cron = m.get("cron")
            if not cron:
                continue
            nxt = _next_occurrence(cron, tzinfo)
            if not nxt:
                continue
            # фильтруем по старт/конечным датам
            if not _meeting_active_on(m, nxt.date()):
                continue
            closest.append((nxt, m))

        if not closest:
            return (
                f"🗓️ Сегодня ({now.strftime('%d.%m.%Y')}, {cfg.get('timezone','Europe/Moscow')}) встреч нет. "
                "Ближайшие пары в расписании не найдены."
            )

        closest.sort(key=lambda x: x[0])
        lines = [
            f"🗓️ Сегодня ({now.strftime('%d.%m.%Y')}, {cfg.get('timezone','Europe/Moscow')}) встреч нет. "
            "Ближайшие в расписании:",
        ]
        for dt, m in closest[:3]:
            lines.append(
                f"• {dt.strftime('%d.%m.%Y %H:%M')} — {m.get('name','Без названия')}"
                f" ({int(m.get('duration_minutes', 45))} мин)"
            )
        return "\n".join(lines)

    items.sort(key=lambda x: x[0])
    lines = [
        f"🗓️ План на сегодня — {now.strftime('%d.%m.%Y')} ({cfg.get('timezone','Europe/Moscow')}):",
    ]
    for dt, m in items:
        name = m.get("name", "Без названия")
        dur = int(m.get("duration_minutes", 45))
        lines.append(
            f"• {dt.strftime('%H:%M')} — {name} ({dur} мин, {_meeting_mode_label(m)}, {_date_range_label(m)})"
        )
    return "\n".join(lines)


def _pick_current_meeting(
    cfg: dict,
) -> Optional[Tuple[dict, datetime, datetime]]:
    """Return (meeting, start, end) only when the meeting is active now."""
    tzinfo = _tz(cfg)
    now = datetime.now(tzinfo)
    for m in cfg.get("meetings", []) or []:
        if not _meeting_active_on(m, now.date()):
            continue
        cron = m.get("cron")
        if not cron:
            continue
        dur = int(m.get("duration_minutes", 45))
        prev = prev_fire_time(cron, tzinfo, now)
        if prev:
            start_dt = prev
            end_dt = start_dt + timedelta(minutes=dur)
            if start_dt <= now <= end_dt:
                return m, start_dt, end_dt
    return None


def _pick_next_meeting(cfg: dict) -> Optional[Tuple[dict, datetime, datetime]]:
    """Return the closest future meeting, respecting start/end dates."""
    tzinfo = _tz(cfg)
    now = datetime.now(tzinfo)
    candidates: List[Tuple[datetime, dict, datetime]] = []
    for m in cfg.get("meetings", []) or []:
        cron = m.get("cron")
        if not cron:
            continue
        dur = int(m.get("duration_minutes", 45))
        nxt = next_fire_time(cron, tzinfo, now)
        if nxt and _meeting_active_on(m, nxt.date()):
            candidates.append((nxt, m, nxt + timedelta(minutes=dur)))
    if not candidates:
        return None
    start_dt, meeting, end_dt = min(candidates, key=lambda item: item[0])
    return meeting, start_dt, end_dt


def _render_home(cfg: dict) -> str:
    dmami = cfg.get("dmami") or {}
    generated = cfg.get("generated_schedule") or {}
    current = _pick_current_meeting(cfg)
    next_item = _pick_next_meeting(cfg)

    lines = [
        "NTS AutoJoin",
        "",
        "Статус:",
        "• сервис работает",
        f"• группа DMAMI: {dmami.get('group') or '-'}",
        f"• timezone: {cfg.get('timezone', 'Europe/Moscow')}",
        "• расписание: "
        + ("сгенерировано" if generated.get("exists") else "не сгенерировано"),
    ]
    if current:
        meeting, start, end = current
        lines.append(
            f"• текущая пара: {start.strftime('%H:%M')}-{end.strftime('%H:%M')} {meeting.get('name', '-')}"
        )
    else:
        lines.append("• текущая пара: нет")
    if next_item:
        meeting, start, _end = next_item
        lines.append(
            f"• следующая пара: {start.strftime('%d.%m %H:%M')} {meeting.get('name', '-')}"
        )
    else:
        lines.append("• следующая пара: нет")
    return "\n".join(lines)


def _render_today(cfg: dict) -> str:
    tzinfo = _tz(cfg)
    now = datetime.now(tzinfo)
    items = []
    for meeting in cfg.get("meetings", []) or []:
        if not _meeting_active_on(meeting, now.date()):
            continue
        cron = meeting.get("cron")
        if not cron:
            continue
        for start in _occurrences_today(cron, tzinfo):
            duration = int(meeting.get("duration_minutes", 45))
            end = start + timedelta(minutes=duration)
            if end < now:
                status = "прошла"
            elif start <= now <= end:
                status = "идёт сейчас"
            else:
                status = "будет позже"
            items.append((start, end, meeting, status))

    lines = [f"Сегодня, {now.strftime('%d.%m.%Y')}"]
    if not items:
        lines.append("Пар на сегодня нет.")
        return "\n".join(lines)

    for start, end, meeting, status in sorted(items, key=lambda item: item[0]):
        lines.append(
            f"• {start.strftime('%H:%M')}-{end.strftime('%H:%M')} "
            f"{meeting.get('name', 'Без названия')} — {status}"
        )
    return "\n".join(lines)

def _render_today(cfg: dict) -> str:
    tzinfo = _tz(cfg)
    now = datetime.now(tzinfo)
    items = []
    for meeting in cfg.get("meetings", []) or []:
        if not _meeting_active_on(meeting, now.date()):
            continue
        cron = meeting.get("cron")
        if not cron:
            continue
        for start in _occurrences_today(cron, tzinfo):
            duration = int(meeting.get("duration_minutes", 45))
            end = start + timedelta(minutes=duration)
            if end < now:
                status = "прошла"
            elif start <= now <= end:
                status = "идёт сейчас"
            else:
                status = "будет"
            items.append((start, end, meeting, status))

    lines = [f"Сегодня, {now.strftime('%d.%m.%Y')}"]
    if not items:
        lines.append("Пар на сегодня нет.")
        return "\n".join(lines)
    for start, end, meeting, status in sorted(items, key=lambda item: item[0]):
        lines.append(
            f"• {start.strftime('%H:%M')}-{end.strftime('%H:%M')} "
            f"{meeting.get('name', 'Без названия')} — {status}; "
            f"{_meeting_mode_label(meeting)}; {_date_range_label(meeting)}"
        )
    return "\n".join(lines)


def _latest_log(pattern: str) -> Optional[Path]:
    logs_dir = get_logs_dir()
    matches = sorted(
        logs_dir.glob(pattern),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return matches[0] if matches else None


def _tail_text(path: Path, max_chars: int = 3500) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Не удалось прочитать {path.name}: {e}"
    if len(text) > max_chars:
        text = text[-max_chars:]
        return f"{path.name} (последние символы):\n{text}"
    return f"{path.name}:\n{text or '<пусто>'}"


def _render_log(kind: str) -> str:
    patterns = {
        "service_stderr": "service_stderr.log",
        "service_stdout": "service_stdout.log",
        "dmami_changes": "dmami_changes_*.txt",
        "dmami_generated": "dmami_generated_*.yaml",
    }
    pattern = patterns.get(kind)
    if not pattern:
        return "Неизвестный лог."
    path = _latest_log(pattern)
    if not path:
        return f"Лог {pattern} пока не найден."
    return _tail_text(path)


HELP = (
    "Доступные команды:\n"
    "/status — план на сегодня\n"
    "/links — ссылки из расписания\n"
    "/shot — скрин активной вкладки (или рабочего стола)\n"
    "/pull — обновить ссылки с DMAMI\n"
    "/reload — перечитать config/schedule.yaml\n"
    "/restart — перезапустить сервис (через systemd, если настроено)\n"
    "/connect — подключиться к текущей/заданной лекции\n\n"
    "/disconnect — выйти из активной конференции\n\n"
    "Синтаксис /connect:\n"
    "• /connect — взять текущую/ближайшую встречу и войти сейчас\n"
    "• /connect HH:MM — взять текущую/ближайшую, посидеть до HH:MM\n"
    "• /connect URL — войти по URL сейчас на 90 минут\n"
    "• /connect URL HH:MM [dur] — войти по URL, выйти в HH:MM (если время прошло — dur минут)\n"
)


# /connect варианты:
CONNECT_FULL = re.compile(
    r"^/connect\s+(\S+)\s+([01]?\d|2[0-3]):([0-5]\d)(?:\s+(\d{2,3}))?\s*$", re.I
)
CONNECT_TIME = re.compile(
    r"^/connect\s+([01]?\d|2[0-3]):([0-5]\d)(?:\s+(\d{2,3}))?\s*$", re.I
)
CONNECT_URL = re.compile(r"^/connect\s+(https?://\S+)\s*$", re.I)


async def run_bot(
    cfg: dict,
    reload_cb: Callable[[], Awaitable[None]],
    screenshot_cb: Callable[[], Awaitable[str | None]],
    dmami_pull_cb: Callable[[], Awaitable[str]],
    connect_cb: Callable[[str, int, int, int], Awaitable[str]],  # url, hh, mm, dur_min
    disconnect_cb: Callable[[], Awaitable[str]],
    logger,
):
    token, default_chat, allowed = _tg(cfg)
    if not token or not default_chat:
        logger.info("TG commands disabled: no token/chat set.")
        return

    try:
        allowed_ids = [int(x) for x in (allowed or [default_chat])]
    except Exception:
        allowed_ids = []

    offset = _read_offset()

    # праймим offset, чтобы не отработали старые команды,
    # и сбрасываем его, если сохранённое значение убежало слишком далеко.
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params={"timeout": 0, "offset": -1}, timeout=10) as r:
                data = await r.json()
        last = max([x["update_id"] for x in data.get("result", [])], default=None)
    except Exception:
        last = None

    if last is not None:
        max_valid_offset = last + 1
        if offset == 0:
            offset = max_valid_offset
        elif offset > max_valid_offset:
            logger.warning(
                "TG offset %s >> last %s; сбрасываем на %s",
                offset,
                last,
                max_valid_offset,
            )
            offset = max_valid_offset

    _write_offset(offset)
    pull_lock = asyncio.Lock()

    async def _cmd_links() -> str:
        c = _load_cfg()
        lines = ["Текущее расписание:"]
        for m in c.get("meetings", []) or []:
            lines.append(f"• {m.get('name','Без имени')}: {m.get('url','<нет url>')}")
        return "\n".join(lines)

    async def _send_menu(chat_id: int, message_id: Optional[int] = None):
        await _edit_or_send(cfg, chat_id, message_id, _render_home(_load_cfg()), _main_keyboard())

    async def _send_today(chat_id: int, message_id: Optional[int] = None):
        keyboard = _kb([[("🔌 Подключиться к текущей", "act:connect")], [("🔙 Назад", "menu:home"), ("🔄 Обновить", "menu:today")]])
        await _edit_or_send(cfg, chat_id, message_id, _render_today(_load_cfg()), keyboard)

    async def _send_logs(chat_id: int, message_id: Optional[int] = None):
        await _edit_or_send(cfg, chat_id, message_id, "Логи", _logs_keyboard())

    async def _run_pull_background(chat_id: int):
        async with pull_lock:
            try:
                msg = await dmami_pull_cb()
            except Exception as e:
                msg = f"DMAMI sync failed: {e}"
            await _send(cfg, msg, chat_id)

    async def _send_or_capture_shot(chat_id: int):
        path = await screenshot_cb()
        if path:
            await send_photo(cfg, path, caption="📷 Текущая вкладка (Playwright)", chat_id=chat_id)
            return
        try:
            from mss import mss

            tzinfo = _tz(cfg)
            tsname = datetime.now(tzinfo).strftime("%Y%m%d-%H%M%S")
            out = get_logs_dir() / f"deskshot_{tsname}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            with mss() as sct:
                sct.shot(output=str(out))
            await send_photo(cfg, str(out), caption="🖥️ Скрин рабочего стола", chat_id=chat_id)
        except Exception as e:
            await _send(cfg, f"Не смог сделать скрин: {e}", chat_id)

    async def _connect_current(chat_id: int) -> None:
        current = _pick_current_meeting(_load_cfg())
        if not current:
            await _send(cfg, "Сейчас нет активной пары.", chat_id)
            return
        meeting, _start, _end = current
        if not meeting.get("url"):
            await _send(
                cfg,
                f"Сейчас идёт пара без ссылки: {meeting.get('name','Без названия')}.\n"
                f"Если она онлайн, пришли ссылку командой: {_connect_hint(meeting)}",
                chat_id,
            )
            return
        now = datetime.now(_tz(cfg))
        msg = await connect_cb(
            meeting["url"],
            now.hour,
            now.minute,
            int(meeting.get("duration_minutes", 90)),
        )
        await _send(cfg, msg, chat_id)

    async def _send_health(chat_id: int) -> None:
        try:
            page = get_any_page()
            if not page:
                await _send(cfg, "Нет активных вкладок.", chat_id)
                return
            ok = await page_is_healthy(page, cfg.get("healthcheck", {}) or {})
            await _send(cfg, f"Текущий статус: {'OK' if ok else 'FAIL'}", chat_id)
        except Exception as e:
            await _send(cfg, f"Не удалось проверить healthcheck: {e}", chat_id)

    async def handle_callback(upd: dict):
        nonlocal cfg
        query = upd.get("callback_query") or {}
        callback_id = query.get("id")
        data = query.get("data") or ""
        msg = query.get("message") or {}
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        message_id = msg.get("message_id")
        ts = int(msg.get("date", 0))
        user_id = int((query.get("from") or {}).get("id") or chat_id or 0)
        is_allowed = not allowed_ids or user_id in allowed_ids or chat_id in allowed_ids
        if not is_allowed:
            await _answer_callback(cfg, callback_id, "Нет доступа.")
            return

        await _answer_callback(cfg, callback_id)
        now_epoch = int(datetime.utcnow().timestamp())
        is_stale = ts and (now_epoch - ts) > STALE_SEC

        if data == "menu:home" or data == "cancel":
            await _send_menu(chat_id, message_id)
            return
        if data == "menu:today":
            await _send_today(chat_id, message_id)
            return
        if data == "menu:logs":
            await _send_logs(chat_id, message_id)
            return
        if data == "act:connect":
            await _connect_current(chat_id)
            return
        if data == "act:shot":
            await _send_or_capture_shot(chat_id)
            return
        if data == "act:health":
            await _send_health(chat_id)
            return
        if data == "act:pull":
            await _edit_or_send(
                cfg,
                chat_id,
                message_id,
                "Обновить расписание из DMAMI?",
                _pull_confirm_keyboard(),
            )
            return
        if data == "confirm:pull":
            if is_stale:
                await _send(cfg, "Игнорирую старую кнопку обновления DMAMI.", chat_id)
                return
            if pull_lock.locked():
                await _send(cfg, "DMAMI sync уже выполняется.", chat_id)
                return
            asyncio.create_task(_run_pull_background(chat_id))
            await _edit_or_send(cfg, chat_id, message_id, "Запустил обновление расписания.", _main_keyboard())
            return
        if data.startswith("log:"):
            kind = data.split(":", 1)[1]
            await _edit_or_send(cfg, chat_id, message_id, _render_log(kind), _logs_keyboard())
            return
        await _send_menu(chat_id, message_id)

    async def handle(chat_id: int, text: str, ts: int, upd_id: int):
        nonlocal cfg

        async def send_reply(message: str):
            await _send(cfg, message, chat_id)

        async def send_photo_reply(path: str, caption: str = ""):
            await send_photo(cfg, path, caption=caption, chat_id=chat_id)

        async def send_help():
            await send_reply(HELP)

        now_epoch = int(datetime.utcnow().timestamp())
        is_stale = (now_epoch - int(ts)) > STALE_SEC
        parts = text.strip().split(maxsplit=2)
        cmd = parts[0].lower()

        is_allowed = not allowed_ids or chat_id in allowed_ids

        if cmd == "/help":
            await send_help()
            return

        if not is_allowed:
            await send_help()
            return

        if cmd in ("/start", "/menu"):
            await _send_menu(chat_id)
            return

        if cmd == "/status":
            await send_reply(_today_summary(_load_cfg()))
            return

        if cmd == "/health":
            # быстрый healthcheck по активной вкладке
            try:
                page = get_any_page()
                if not page:
                    await send_reply("Нет активных вкладок.")
                    return
                ok = await page_is_healthy(page, cfg.get("healthcheck", {}) or {})
                await send_reply(f"Текущий статус: {'OK' if ok else 'FAIL'}")
            except Exception as e:
                await send_reply(f"Не удалось проверить здоровье: {e}")
            return

        if cmd == "/links":
            await send_reply(await _cmd_links())
            return

        if cmd == "/pull":
            if is_stale:
                await send_reply("Ignoring stale /pull command.")
                return
            if pull_lock.locked():
                await send_reply("DMAMI pull already running.")
                return

            asyncio.create_task(_run_pull_background(chat_id))
            await send_reply("DMAMI pull started. I will send the result when it finishes.")
            return

        if cmd == "/reload":
            if is_stale:
                await send_reply("⏭️ Игнорирую старую команду /reload.")
                return
            await reload_cb()
            await send_reply("♻️ Конфиг перечитан и перепланирован.")
            return

        if cmd == "/disconnect":
            if is_stale:
                await send_reply("⏭️ Игнорирую старую команду /disconnect.")
                return
            msg = await disconnect_cb()
            await send_reply(msg)
            return

        if cmd == "/shot":
            path = await screenshot_cb()
            if path:
                await send_photo_reply(path, caption="📷 Текущая вкладка (Playwright)")
                return
            # fallback — десктоп
            try:
                from mss import mss

                tzinfo = _tz(cfg)
                tsname = datetime.now(tzinfo).strftime("%Y%m%d-%H%M%S")
                out = get_logs_dir() / f"deskshot_{tsname}.png"
                out.parent.mkdir(parents=True, exist_ok=True)
                with mss() as sct:
                    sct.shot(output=str(out))
                await send_photo_reply(str(out), caption="🖥️ Скрин рабочего стола (fallback)")
            except Exception as e:
                await send_reply(f"📷 Не смог снять десктоп: {e}")
            return

        if cmd == "/link":
            if is_stale:
                await send_reply("Игнорирую старую команду /link.")
                return
            url = parts[1].strip() if len(parts) > 1 else ""
            if not re.match(r"^https?://\S+$", url, re.I):
                await send_reply("Использование: /link <url>")
                return
            pick = _pick_current_meeting(_load_cfg())
            if not pick:
                await send_reply("Сейчас нет активной пары для этой ссылки.")
                return
            meeting, _start, end = pick
            if meeting.get("url"):
                await send_reply("У текущей пары уже есть ссылка. Используй /connect <url> HH:MM, если нужна другая.")
                return
            msg = await connect_cb(url, end.hour, end.minute, int(meeting.get("duration_minutes", 90)))
            await send_reply(msg)
            return

        if cmd == "/connect":
            if is_stale:
                await send_reply("⏭️ Игнорирую старую команду /connect.")
                return

            m_full = CONNECT_FULL.match(text)
            m_time = CONNECT_TIME.match(text)
            m_url = CONNECT_URL.match(text)

            tzinfo = _tz(cfg)

            if m_full:
                url, hh, mm, dur = (
                    m_full.group(1),
                    int(m_full.group(2)),
                    int(m_full.group(3)),
                    m_full.group(4),
                )
                dur_min = int(dur) if dur else 90
                msg = await connect_cb(url, hh, mm, dur_min)
                await send_reply(msg)
                return

            if m_url:
                # только URL → стартуем сейчас, длительность по умолчанию 90
                url = m_url.group(1)
                now = datetime.now(tzinfo)
                msg = await connect_cb(url, now.hour, now.minute, 90)
                await send_reply(msg)
                return

            if m_time:
                # только HH:MM → подбираем ссылку из текущей/ближайшей встречи
                hh, mm = int(m_time.group(1)), int(m_time.group(2))
                dur = int(m_time.group(3) or 90)
                pick = _pick_current_meeting(_load_cfg())
                if not pick:
                    await send_reply("Не нашёл подходящую встречу в расписании.")
                    return
                m, start, end = pick
                if not m.get("url"):
                    await send_reply(
                        f"Сейчас идёт пара без ссылки: {m.get('name','Без названия')}.\n"
                        f"Пришли ссылку командой: {_connect_hint(m)}"
                    )
                    return
                msg = await connect_cb(m["url"], hh, mm, dur)
                await send_reply(msg)
                return

            # ничего не подошло → берём текущую/ближайшую встречу и стартуем сейчас
            pick = _pick_current_meeting(_load_cfg())
            if not pick:
                await send_reply(
                    "Не понял параметры. Использование: /connect <url?> <HH:MM?> [длит_мин]",
                )
                return
            m, start, end = pick
            if not m.get("url"):
                await send_reply(
                    f"Сейчас идёт пара без ссылки: {m.get('name','Без названия')}.\n"
                    f"Пришли ссылку командой: {_connect_hint(m)}"
                )
                return
            now = datetime.now(tzinfo)
            msg = await connect_cb(
                m["url"], now.hour, now.minute, int(m.get("duration_minutes", 90))
            )
            await send_reply(msg)
            return

        if cmd == "/restart":
            # эту команду ты можешь повесить на systemd-юнит через shell-скрипт,
            # здесь можно просто залогировать/проигнорировать
            await send_reply("Команда /restart пока не реализована.")
            return

        await send_help()

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"timeout": 20, "offset": offset}
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params=params, timeout=30) as r:
                    data = await r.json()
            if not data.get("ok"):
                await asyncio.sleep(3)
                continue

            for upd in data.get("result", []):
                upd_id = int(upd["update_id"])
                try:
                    if upd.get("callback_query"):
                        try:
                            await handle_callback(upd)
                        except Exception:
                            query = upd.get("callback_query") or {}
                            await _answer_callback(cfg, query.get("id"), "Callback error.")
                            logger.exception("TG callback error")
                        continue
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    chat_id = int(msg["chat"]["id"])
                    ts = int(msg.get("date", 0))
                    if "text" in msg:
                        try:
                            await handle(chat_id, msg["text"], ts, upd_id)
                        except Exception:
                            logger.exception("TG message error")
                finally:
                    offset = max(offset, upd_id + 1)
                    _write_offset(offset)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"TG poll error: {e}")
            await asyncio.sleep(2)
