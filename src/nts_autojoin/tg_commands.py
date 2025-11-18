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

OFFSET_FILE = Path("logs/tg_offset.state")
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


def _load_cfg() -> dict:
    with open("config/schedule.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _tg(cfg):
    n = cfg.get("notify") or {}
    return n.get("telegram_bot_token"), n.get("telegram_chat_id")


async def _send(cfg, text: str):
    token, chat = _tg(cfg)
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=data, timeout=60) as r:
                await r.text()
    except Exception:
        pass


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
        return f"🗓️ Сегодня ({now.strftime('%d.%m.%Y')}, {cfg.get('timezone','Europe/Moscow')}) встреч нет."

    items.sort(key=lambda x: x[0])
    lines = [
        f"🗓️ План на сегодня — {now.strftime('%d.%m.%Y')} ({cfg.get('timezone','Europe/Moscow')}):"
    ]
    for dt, m in items:
        name = m.get("name", "Без названия")
        dur = int(m.get("duration_minutes", 45))
        lines.append(f"• {dt.strftime('%H:%M')} — {name} ({dur} мин)")
    return "\n".join(lines)


def _pick_current_meeting(
    cfg: dict,
) -> Optional[Tuple[dict, datetime, datetime]]:
    """Вернуть (meeting, start, end), если сейчас идёт; иначе ближайшую будущую."""
    tzinfo = _tz(cfg)
    now = datetime.now(tzinfo)
    cand = None
    cand_start = cand_end = None
    for m in cfg.get("meetings", []) or []:
        if not _meeting_active_on(m, now.date()):
            continue
        cron = m.get("cron")
        if not cron:
            continue
        dur = int(m.get("duration_minutes", 45))
        trig = CronTrigger.from_crontab(cron, timezone=tzinfo)
        prev = trig.get_prev_fire_time(None, now)
        if prev:
            start = prev
            end = start + timedelta(minutes=dur)
            if start <= now <= end:
                return m, start, end
        nxt = trig.get_next_fire_time(None, now)
        if nxt and cand is None:
            cand, cand_start, cand_end = m, nxt, nxt + timedelta(minutes=dur)
    if cand:
        return cand, cand_start, cand_end
    return None


HELP = (
    "Доступные команды:\n"
    "/status — план на сегодня\n"
    "/links — ссылки из расписания\n"
    "/shot — скрин активной вкладки (или рабочего стола)\n"
    "/pull — обновить ссылки с DMAMI\n"
    "/reload — перечитать config/schedule.yaml\n"
    "/restart — перезапустить сервис (через systemd, если настроено)\n"
    "/connect — подключиться к текущей/заданной лекции\n\n"
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
    if offset == 0:
        # праймим offset, чтобы не отработали старые команды
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params={"timeout": 0, "offset": -1}, timeout=10) as r:
                    data = await r.json()
            last = max([x["update_id"] for x in data.get("result", [])], default=None)
            offset = (last + 1) if last is not None else 0
        except Exception:
            offset = 0
        _write_offset(offset)

    async def _cmd_links() -> str:
        c = _load_cfg()
        lines = ["Текущее расписание:"]
        for m in c.get("meetings", []) or []:
            lines.append(f"• {m.get('name','Без имени')}: {m.get('url','<нет url>')}")
        return "\n".join(lines)

    async def handle(chat_id: int, text: str, ts: int, upd_id: int):
        nonlocal cfg

        if allowed_ids and chat_id not in allowed_ids:
            return

        now_epoch = int(datetime.utcnow().timestamp())
        is_stale = (now_epoch - int(ts)) > STALE_SEC
        parts = text.strip().split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd in ("/help", "/start"):
            await _send(cfg, HELP)
            return

        if cmd == "/status":
            await _send(cfg, _today_summary(_load_cfg()))
            return

        if cmd == "/health":
            # быстрый healthcheck по активной вкладке
            try:
                page = get_any_page()
                if not page:
                    await _send(cfg, "Нет активных вкладок.")
                    return
                ok = await page_is_healthy(page, cfg.get("healthcheck", {}) or {})
                await _send(cfg, f"Текущий статус: {'OK' if ok else 'FAIL'}")
            except Exception as e:
                await _send(cfg, f"Не удалось проверить здоровье: {e}")
            return

        if cmd == "/links":
            await _send(cfg, await _cmd_links())
            return

        if cmd == "/pull":
            if is_stale:
                await _send(cfg, "⏭️ Игнорирую старую команду /pull.")
                return
            msg = await dmami_pull_cb()
            await _send(cfg, msg)
            return

        if cmd == "/reload":
            if is_stale:
                await _send(cfg, "⏭️ Игнорирую старую команду /reload.")
                return
            await reload_cb()
            await _send(cfg, "♻️ Конфиг перечитан и перепланирован.")
            return

        if cmd == "/shot":
            path = await screenshot_cb()
            if path:
                await send_photo(cfg, path, caption="📷 Текущая вкладка (Playwright)")
                return
            # fallback — десктоп
            try:
                from mss import mss

                tzinfo = _tz(cfg)
                tsname = datetime.now(tzinfo).strftime("%Y%m%d-%H%M%S")
                Path("logs").mkdir(parents=True, exist_ok=True)
                out = f"logs/deskshot_{tsname}.png"
                with mss() as sct:
                    sct.shot(output=out)
                await send_photo(cfg, out, caption="🖥️ Скрин рабочего стола (fallback)")
            except Exception as e:
                await _send(cfg, f"📷 Не смог снять десктоп: {e}")
            return

        if cmd == "/connect":
            if is_stale:
                await _send(cfg, "⏭️ Игнорирую старую команду /connect.")
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
                await _send(cfg, msg)
                return

            if m_url:
                # только URL → стартуем сейчас, длительность по умолчанию 90
                url = m_url.group(1)
                now = datetime.now(tzinfo)
                msg = await connect_cb(url, now.hour, now.minute, 90)
                await _send(cfg, msg)
                return

            if m_time:
                # только HH:MM → подбираем ссылку из текущей/ближайшей встречи
                hh, mm = int(m_time.group(1)), int(m_time.group(2))
                dur = int(m_time.group(3) or 90)
                pick = _pick_current_meeting(_load_cfg())
                if not pick:
                    await _send(cfg, "Не нашёл подходящую встречу в расписании.")
                    return
                m, start, end = pick
                msg = await connect_cb(m["url"], hh, mm, dur)
                await _send(cfg, msg)
                return

            # ничего не подошло → берём текущую/ближайшую встречу и стартуем сейчас
            pick = _pick_current_meeting(_load_cfg())
            if not pick:
                await _send(
                    cfg,
                    "Не понял параметры. Использование: /connect <url?> <HH:MM?> [длит_мин]",
                )
                return
            m, start, end = pick
            now = datetime.now(tzinfo)
            msg = await connect_cb(
                m["url"], now.hour, now.minute, int(m.get("duration_minutes", 90))
            )
            await _send(cfg, msg)
            return

        if cmd == "/restart":
            # эту команду ты можешь повесить на systemd-юнит через shell-скрипт,
            # здесь можно просто залогировать/проигнорировать
            await _send(cfg, "Команда /restart пока не реализована.")
            return

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"timeout": 50, "offset": offset}
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params=params, timeout=60) as r:
                    data = await r.json()
            if not data.get("ok"):
                await asyncio.sleep(3)
                continue

            max_id = None
            for upd in data.get("result", []):
                upd_id = upd["update_id"]
                max_id = upd_id if (max_id is None or upd_id > max_id) else max_id
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = int(msg["chat"]["id"])
                ts = int(msg.get("date", 0))
                if "text" in msg:
                    await handle(chat_id, msg["text"], ts, upd_id)

            if max_id is not None:
                offset = max_id + 1
                _write_offset(offset)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"TG poll error: {e}")
            await asyncio.sleep(2)
