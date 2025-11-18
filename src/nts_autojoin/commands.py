import asyncio, os, re
from typing import Dict, Optional
import aiohttp
from .state import STATE
from .notifier import notify, send_photo
from .screens import take_screenshot
from .scheduler import run_meeting
from dateutil import tz
from datetime import datetime, timedelta
from apscheduler.triggers.cron import CronTrigger

def parse_connect_args(text: str):
    parts = text.strip().split()
    url = None; when = None
    for p in parts[1:]:
        if p.startswith("http"): url = p
        elif re.match(r"^\d{1,2}:\d{2}$", p): when = p
    return url, when

def pick_current_meeting(cfg: Dict, now, logger):
    tzname = cfg.get("timezone","Europe/Moscow")
    for m in (cfg.get("meetings") or []):
        cron_expr = m.get("cron"); dur = int(m.get("duration_minutes", 60))
        if not cron_expr: continue
        trig = CronTrigger.from_crontab(cron_expr, timezone=tz.gettz(tzname))
        prev_fire = trig.get_prev_fire_time(None, now)
        if prev_fire:
            start = prev_fire; end = start + timedelta(minutes=dur)
            if start <= now <= end: return m
    return (cfg.get("meetings") or [None])[0]

class TelegramCommands:
    def __init__(self, cfg: Dict, logger):
        self.cfg = cfg; self.logger = logger
        n = cfg.get("notify", {}) or {}
        self.token = n.get("telegram_bot_token"); self.chat_id = n.get("telegram_chat_id")
        self.last_update_id = 0

    async def poll(self):
        if not self.token or not self.chat_id: return
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"timeout": 0, "offset": self.last_update_id + 1}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params=params, timeout=10) as r:
                    data = await r.json()
                    for upd in data.get("result", []):
                        self.last_update_id = max(self.last_update_id, upd["update_id"])
                        await self.handle_update(upd)
        except Exception: pass

    async def handle_update(self, upd):
        msg = upd.get("message") or upd.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        if not text or str(msg.get("chat", {}).get("id")) != str(self.chat_id): return
        if text.startswith("/connect"):
            url_arg, when_arg = parse_connect_args(text)
            await notify(self.cfg, f"⏳ /connect получен. url={url_arg or '-'} time={when_arg or '-'}")
            if url_arg:
                target = {"name":"AdHoc","url":url_arg,"duration_minutes":60,"join":(self.cfg.get("meetings") or [{}])[0].get("join", {})}
            else:
                now = datetime.now(tz.gettz(self.cfg.get("timezone","Europe/Moscow")))
                target = pick_current_meeting(self.cfg, now, self.logger)
                if not target: 
                    await notify(self.cfg, "Не нашёл встречу по времени. Проверь cron в конфиге."); return
            if when_arg:
                hh, mm = map(int, when_arg.split(":"))
                now = datetime.now(tz.gettz(self.cfg.get("timezone","Europe/Moscow")))
                start_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if start_at >= now:
                    await notify(self.cfg, f"⏳ Жду до {when_arg} и подключаюсь…")
                    await asyncio.sleep((start_at-now).total_seconds())
                else:
                    await notify(self.cfg, f"⌛ {when_arg} уже прошло — стартую сейчас.")
            await run_meeting(target, self.cfg.get("timezone","Europe/Moscow"),
                              self.cfg.get("chromium",{}), self.cfg.get("healthcheck",{}),
                              self.cfg, self.logger)
        elif text.startswith("/shot"):
            if STATE.page:
                path = await take_screenshot(STATE.page, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs"))
                if path: await send_photo(self.cfg, path, caption=f"Скрин '{STATE.name}'")
                else: await notify(self.cfg, "Не удалось сделать скрин (нет активной вкладки).")
            else: await notify(self.cfg, "Нет активной встречи.")
        elif text.startswith("/now"):
            if STATE.active:
                ok = STATE.last_health_ok; when = STATE.last_health_ts.strftime("%H:%M:%S") if STATE.last_health_ts else "—"
                await notify(self.cfg, f"Сейчас в '{STATE.name}'. Последний пинг {when}, статус: {'OK' if ok else 'FAIL'}")
            else: await notify(self.cfg, "Сейчас ни к одной встрече не подключён.")
