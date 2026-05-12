# -*- coding: utf-8 -*-
import asyncio, json
from pathlib import Path
from datetime import datetime
from dateutil import tz

from nts_autojoin.mospoly_scraper import fetch_mospoly_schedule, to_yaml_mapping
from nts_autojoin.settings import get_logs_dir, get_secrets_dir, load_config
from nts_autojoin.notifier import notify, send_document

async def main():
    cfg = load_config()
    cookies_path = get_secrets_dir() / "mospoly_cookies.json"
    cookies_path.parent.mkdir(parents=True, exist_ok=True)

    if not cookies_path.exists():
        raise SystemExit(f"Положи куки в {cookies_path} (JSON из DevTools/расширения).")

    chrome = (cfg.get("chromium") or {}).get("executable_path")

    # важное: Chrome в headless у тебя падал; запускаем НЕ headless
    events = await fetch_mospoly_schedule(str(cookies_path), headless=False, chromium_executable=chrome)

    ts = datetime.now(tz.gettz(cfg.get("timezone", "Europe/Moscow"))).strftime("%Y%m%d-%H%M%S")
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_json = logs_dir / f"mospoly_schedule_{ts}.json"
    out_yaml = logs_dir / f"mospoly_links_{ts}.yaml"

    out_json.write_text(json.dumps([e.__dict__ for e in events], ensure_ascii=False, indent=2), encoding="utf-8")
    out_yaml.write_text(to_yaml_mapping(events), encoding="utf-8")

    msg = f"📥 Парсинг расписания: найдено {len(events)} ссылок Webinar.\n" \
          f"JSON: {out_json.name}\nYAML: {out_yaml.name}"
    print(msg)

    try:
        await notify(cfg, msg)
        await send_document(cfg, str(out_json), caption="mospoly_schedule.json")
        await send_document(cfg, str(out_yaml), caption="mospoly_links.yaml")
    except Exception:
        pass

if __name__ == "__main__":
    asyncio.run(main())
