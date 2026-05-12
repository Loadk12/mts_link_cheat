from datetime import datetime, timedelta
from typing import Optional

from apscheduler.triggers.cron import CronTrigger


def prev_fire_time(
    cron_expr: str,
    tzinfo,
    now: datetime,
    lookback_days: int = 14,
    max_iterations: int = 1000,
) -> Optional[datetime]:
    trigger = CronTrigger.from_crontab(cron_expr, timezone=tzinfo)
    start = now - timedelta(days=lookback_days)
    prev = None
    cur = start
    last = None

    for _ in range(max_iterations):
        nxt = trigger.get_next_fire_time(prev, cur)
        if not nxt or nxt > now:
            return last
        last = nxt
        prev = nxt
        cur = nxt + timedelta(seconds=1)

    return last


def next_fire_time(cron_expr: str, tzinfo, now: datetime) -> Optional[datetime]:
    trigger = CronTrigger.from_crontab(cron_expr, timezone=tzinfo)
    return trigger.get_next_fire_time(None, now)
