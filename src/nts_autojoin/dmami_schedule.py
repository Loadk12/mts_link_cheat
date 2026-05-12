from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import yaml

from .dmami_scraper import WebEvent, fetch_dmami
from .settings import get_generated_config_path, get_logs_dir


DAY_ALIASES = {
    "mon": ("mon", "monday", "пн", "понедельник"),
    "tue": ("tue", "tuesday", "вт", "вторник"),
    "wed": ("wed", "wednesday", "ср", "среда"),
    "thu": ("thu", "thursday", "чт", "четверг"),
    "fri": ("fri", "friday", "пт", "пятница"),
    "sat": ("sat", "saturday", "сб", "суббота"),
    "sun": ("sun", "sunday", "вс", "воскресенье"),
}

LESSON_TYPES = {
    "lecture": ("лек", "лекция", "lecture"),
    "practice": ("прак", "практика", "семинар", "seminar", "practice"),
    "lab": ("лаб", "лаборатор", "lab"),
}

DATE_RE = re.compile(r"(?P<day>\d{1,2})[./](?P<month>\d{1,2})(?:[./](?P<year>\d{2,4}))?")


def normalize_title(value: str) -> str:
    value = (value or "").strip().lower().replace("ё", "е")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\s-]+", "", value, flags=re.UNICODE)
    return value.strip()


def normalize_weekday(value: str) -> str:
    text = normalize_title(value)
    for key, aliases in DAY_ALIASES.items():
        if any(alias in text for alias in aliases):
            return key
    return text[:3] if text else ""


def normalize_lesson_type(value: str, title: str = "", raw_text: str = "") -> str:
    text = normalize_title(" ".join([value or "", title or "", raw_text or ""]))
    for key, aliases in LESSON_TYPES.items():
        if any(alias in text for alias in aliases):
            return key
    return ""


def display_lesson_type(value: str) -> str:
    return {
        "lecture": "Лекция",
        "practice": "Практика",
        "lab": "Лаб. работа",
    }.get(value, "")


def dmami_key(event: WebEvent) -> str:
    title = normalize_title(event.title)
    lesson_type = normalize_lesson_type(event.lesson_type, event.title, event.raw_text)
    weekday = normalize_weekday(event.day)
    parts = [title]
    if lesson_type:
        parts.append(lesson_type)
    parts.extend([weekday, event.start])
    return "|".join(part for part in parts if part)


def _parse_event_date(event: WebEvent, cfg: Dict[str, Any]) -> str:
    if event.date:
        return str(event.date)
    source = " ".join([event.day or "", event.raw_text or ""])
    match = DATE_RE.search(source)
    if not match:
        return ""
    day = int(match.group("day"))
    month = int(match.group("month"))
    year_raw = match.group("year")
    if year_raw:
        year = int(year_raw)
        if year < 100:
            year += 2000
    else:
        year = int((cfg.get("dmami") or {}).get("academic_year") or datetime.now().year)
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _duration_minutes(start: str, end: str) -> int:
    try:
        sh, sm = [int(x) for x in start.split(":", 1)]
        eh, em = [int(x) for x in end.split(":", 1)]
    except Exception:
        return 90
    return max(1, (eh * 60 + em) - (sh * 60 + sm))


def _cron(start: str, weekday: str) -> str:
    hour, minute = [int(x) for x in start.split(":", 1)]
    return f"{minute} {hour} * * {weekday}"


def _meeting_name(event: WebEvent) -> str:
    lesson_type = normalize_lesson_type(event.lesson_type, event.title, event.raw_text)
    display_type = display_lesson_type(lesson_type)
    return f"{event.title} ({display_type})" if display_type else event.title


def build_generated_meetings(
    events: Iterable[WebEvent], cfg: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    grouped: Dict[str, List[WebEvent]] = {}
    skipped: List[Dict[str, Any]] = []

    for event in events:
        key = dmami_key(event)
        weekday = normalize_weekday(event.day)
        if not key or not weekday or not event.start or not event.end or not event.href:
            skipped.append({"event": asdict(event), "reason": "missing key/time/url"})
            continue
        grouped.setdefault(key, []).append(event)

    dmami_cfg = cfg.get("dmami") or {}
    default_start = dmami_cfg.get("default_start_date")
    default_end = dmami_cfg.get("default_end_date")

    meetings: List[Dict[str, Any]] = []
    for key, items in sorted(grouped.items(), key=lambda item: item[0]):
        first = items[0]
        weekday = normalize_weekday(first.day)
        dates = sorted({d for d in (_parse_event_date(e, cfg) for e in items) if d})
        urls = [e.href for e in items if e.href]
        url = urls[-1] if urls else first.href
        meeting: Dict[str, Any] = {
            "name": _meeting_name(first),
            "url": url,
            "cron": _cron(first.start, weekday),
            "duration_minutes": _duration_minutes(first.start, first.end),
            "dmami_key": key,
            "dmami": {
                "title": first.title,
                "lesson_type": normalize_lesson_type(
                    first.lesson_type, first.title, first.raw_text
                ),
                "weekday": weekday,
                "start": first.start,
                "end": first.end,
                "teacher": first.teacher,
                "room": first.room,
                "raw_text": first.raw_text,
            },
        }
        if dates:
            meeting["start_date"] = min(dates)
            meeting["end_date"] = max(dates)
        elif default_start or default_end:
            if default_start:
                meeting["start_date"] = default_start
            if default_end:
                meeting["end_date"] = default_end
            meeting["dmami"]["dates_source"] = "config defaults"
        else:
            meeting["dmami"]["dates_source"] = "not detected"
        meetings.append(meeting)

    return meetings, skipped


def _read_generated(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _index_by_key(meetings: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(m.get("dmami_key")): m
        for m in meetings
        if isinstance(m, dict) and m.get("dmami_key")
    }


def _write_yaml_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _changes_text(
    old: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]], skipped: List[Dict[str, Any]]
) -> str:
    old_keys = set(old)
    new_keys = set(new)
    lines = [
        f"new keys: {len(new_keys - old_keys)}",
        f"removed keys: {len(old_keys - new_keys)}",
        f"updated urls: {sum(1 for key in old_keys & new_keys if old[key].get('url') != new[key].get('url'))}",
        f"skipped events: {len(skipped)}",
        "",
    ]
    for key in sorted(new_keys - old_keys):
        lines.append(f"+ {key}")
    for key in sorted(old_keys - new_keys):
        lines.append(f"- {key}")
    for key in sorted(old_keys & new_keys):
        if old[key].get("url") != new[key].get("url"):
            lines.append(f"~ {key}")
    if skipped:
        lines.append("")
        lines.append("skipped:")
        for item in skipped:
            event = item.get("event") or {}
            lines.append(f"! {item.get('reason')}: {event.get('title')} {event.get('day')} {event.get('start')}")
    return "\n".join(lines).strip() + "\n"


async def sync_dmami_schedule(cfg: Dict[str, Any], logger) -> Dict[str, Any]:
    dmami_cfg = cfg.get("dmami") or {}
    group = (dmami_cfg.get("group") or "").strip()
    if not group:
        raise RuntimeError("В конфиге не задан dmami.group")

    logger.info("[dmami] syncing generated schedule for group %s", group)
    events = await fetch_dmami(
        group,
        headless=True,
        chrome=(cfg.get("chromium") or {}).get("executable_path"),
    )
    meetings, skipped = build_generated_meetings(events, cfg)

    generated_path = get_generated_config_path()
    previous = _read_generated(generated_path)
    old_index = _index_by_key(previous.get("meetings") or [])
    new_index = _index_by_key(meetings)

    now = datetime.now().astimezone()
    payload = {
        "generated_by": "nts_autojoin.dmami",
        "generated_at": now.isoformat(timespec="seconds"),
        "source": {
            "group": group,
            "raw_events": len(events),
            "dates_detected": any(
                bool(m.get("start_date") or m.get("end_date")) for m in meetings
            ),
        },
        "meetings": meetings,
    }
    _write_yaml_atomic(generated_path, payload)

    logs_dir = get_logs_dir()
    ts = now.strftime("%Y%m%d-%H%M%S")
    raw_path = logs_dir / f"dmami_raw_{ts}.yaml"
    generated_log_path = logs_dir / f"dmami_generated_{ts}.yaml"
    changes_path = logs_dir / f"dmami_changes_{ts}.txt"
    unmatched_path = logs_dir / f"dmami_unmatched_{ts}.yaml"

    _write_yaml_atomic(
        raw_path,
        {"events": [asdict(event) for event in events], "skipped": skipped},
    )
    _write_yaml_atomic(generated_log_path, payload)
    _write_text(changes_path, _changes_text(old_index, new_index, skipped))
    if skipped:
        _write_yaml_atomic(unmatched_path, {"skipped": skipped})

    return {
        "raw_events": len(events),
        "generated_meetings": len(meetings),
        "new_keys": len(set(new_index) - set(old_index)),
        "removed_keys": len(set(old_index) - set(new_index)),
        "updated_urls": sum(
            1
            for key in set(old_index) & set(new_index)
            if old_index[key].get("url") != new_index[key].get("url")
        ),
        "skipped": len(skipped),
        "generated_path": str(generated_path),
        "raw_path": str(raw_path),
        "generated_log_path": str(generated_log_path),
        "changes_path": str(changes_path),
        "unmatched_path": str(unmatched_path) if skipped else "",
    }


def format_sync_report(result: Dict[str, Any]) -> str:
    lines = [
        "DMAMI sync завершён.",
        f"Raw events: {result['raw_events']}",
        f"Meetings generated: {result['generated_meetings']}",
        f"New keys: {result['new_keys']}",
        f"Removed keys: {result['removed_keys']}",
        f"Updated URLs: {result['updated_urls']}",
        f"Skipped: {result['skipped']}",
        "",
        f"Generated: {result['generated_path']}",
        f"Raw log: {result['raw_path']}",
        f"Generated log: {result['generated_log_path']}",
        f"Changes: {result['changes_path']}",
    ]
    if result.get("unmatched_path"):
        lines.append(f"Unmatched: {result['unmatched_path']}")
    return "\n".join(lines)
