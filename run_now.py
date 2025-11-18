import sys, asyncio
sys.path.append('src')

from nts_autojoin.settings import load_config
from nts_autojoin.logging_setup import setup_logger
from nts_autojoin.scheduler import run_meeting

def pick_meeting(cfg, name_fragment=None):
    meetings = cfg.get("meetings", [])
    if not meetings:
        raise SystemExit("В конфиге нет meetings.")
    if name_fragment:
        low = name_fragment.lower()
        for m in meetings:
            if low in m.get("name","").lower():
                return m
        print("Не нашёл по фрагменту имени, беру первую встречу.")
    return meetings[0]

async def main():
    cfg = load_config()
    tzname = cfg.get("timezone", "Europe/Moscow")
    chromium_cfg = cfg.get("chromium", {}) or {}
    hc_cfg = cfg.get("healthcheck", {}) or {}
    logger = setup_logger()
    meeting = pick_meeting(cfg, name_fragment=None)  # можно подставить фрагмент названия
    await run_meeting(meeting, tzname, chromium_cfg, hc_cfg, cfg, logger)

if __name__ == "__main__":
    asyncio.run(main())
