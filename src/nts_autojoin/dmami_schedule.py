from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "сент": 9, "окт": 10, "ноя": 11, "дек": 12,
}
MONTH_RE = r"Янв|Фев|Мар|Апр|Май|Мая|Июн|Июл|Авг|Сен|Сент|Окт|Ноя|Дек"
RANGE_RE = re.compile(
    rf"(?P<d1>\d{{1,2}})\s*(?P<m1>{MONTH_RE})\.?\s*[-–—]\s*"
    rf"(?P<d2>\d{{1,2}})\s*(?P<m2>{MONTH_RE})\.?(?:\s*(?P<y2>\d{{4}}))?",
    re.UNICODE | re.IGNORECASE,
)


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
    text = normalize_title(" ".join([value or "", title or ""]))
    for key, aliases in LESSON_TYPES.items():
        if any(alias in text for alias in aliases):
            return key
    return ""


def display_lesson_type(value: str) -> str:
    return {"lecture": "Лекция", "practice": "Практика", "lab": "Лаб. работа"}.get(value, "")


def parse_russian_month(value: str) -> Optional[int]:
    text = normalize_title(value).replace(".", "")
    return MONTHS.get(text[:4]) or MONTHS.get(text[:3])


def parse_dmami_date_range(raw: str, reference_date: date | datetime | None = None) -> Tuple[Optional[date], Optional[date]]:
    ref = reference_date.date() if isinstance(reference_date, datetime) else (reference_date or date.today())
    match = RANGE_RE.search(raw or "")
    if not match:
        return None, None
    start_month = parse_russian_month(match.group("m1"))
    end_month = parse_russian_month(match.group("m2"))
    if not start_month or not end_month:
        return None, None
    start_year = int(match.group("y2")) if match.group("y2") else ref.year
    if not match.group("y2") and ref.month <= 2 and start_month >= 9:
        start_year -= 1
    end_year = start_year + (1 if end_month < start_month else 0)
    try:
        return date(start_year, start_month, int(match.group("d1"))), date(end_year, end_month, int(match.group("d2")))
    except ValueError:
        return None, None


def is_date_in_range(day: date, start_date: date | None, end_date: date | None) -> bool:
    return not ((start_date and day < start_date) or (end_date and day > end_date))


def dmami_key(event: WebEvent) -> str:
    lesson_type = normalize_lesson_type(event.lesson_type, event.title, event.raw_text)
    parts = [normalize_title(event.title)]
    if lesson_type:
        parts.append(lesson_type)
    parts.extend([normalize_weekday(event.day), event.start])
    return "|".join(part for part in parts if part)


def _duration_minutes(start: str, end: str) -> int:
    try:
        sh, sm = [int(x) for x in start.split(":", 1)]
        eh, em = [int(x) for x in end.split(":", 1)]
        return max(1, (eh * 60 + em) - (sh * 60 + sm))
    except Exception:
        return 90


def _cron(start: str, weekday: str) -> str:
    hour, minute = [int(x) for x in start.split(":", 1)]
    return f"{minute} {hour} * * {weekday}"


def _url_kind(url: str | None) -> str:
    if not url:
        return "none"
    return "mts_link" if "my.mts-link.ru" in url.lower() else "external_link"


def _meeting_mode(event: WebEvent, url: str | None) -> str:
    if _url_kind(url) == "mts_link":
        return "online_auto"
    if url:
        return "online_manual"
    if event.room:
        return "offline"
    return "online_manual"


def _meeting_name(event: WebEvent) -> str:
    lesson_type = normalize_lesson_type(event.lesson_type, event.title, event.raw_text)
    label = display_lesson_type(lesson_type)
    if label and label.lower() not in event.title.lower():
        return f"{event.title} ({label})"
    return event.title


def build_generated_meetings(
    events: Iterable[WebEvent], cfg: Dict[str, Any], reference_date: date | None = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    ref = reference_date or datetime.now().date()
    grouped: Dict[str, List[WebEvent]] = {}
    skipped: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for event in events:
        key = dmami_key(event)
        if not key or not normalize_weekday(event.day) or not event.start or not event.end:
            skipped.append({"event": asdict(event), "reason": "missing key/time"})
            continue
        grouped.setdefault(key, []).append(event)

    meetings: List[Dict[str, Any]] = []
    defaults = cfg.get("dmami") or {}
    for key, items in sorted(grouped.items(), key=lambda item: item[0]):
        first = items[0]
        ranges = [parse_dmami_date_range(e.date_range_raw or e.raw_text, ref) for e in items]
        starts = [s for s, _e in ranges if s]
        ends = [e for _s, e in ranges if e]
        start_text = min(starts).isoformat() if starts else defaults.get("default_start_date")
        end_text = max(ends).isoformat() if ends else defaults.get("default_end_date")
        if not start_text or not end_text:
            warnings.append(f"date range not detected: {first.title} {first.day} {first.start}")
        url = next((e.href for e in reversed(items) if e.href), None)
        weekday = normalize_weekday(first.day)
        lesson_type = normalize_lesson_type(first.lesson_type, first.title, first.raw_text)
        meetings.append({
            "name": _meeting_name(first),
            "url": url,
            "cron": _cron(first.start, weekday),
            "duration_minutes": _duration_minutes(first.start, first.end),
            "start_date": start_text,
            "end_date": end_text,
            "dmami_key": key,
            "source": "dmami",
            "meeting_mode": _meeting_mode(first, url),
            "dmami": {
                "title": first.title,
                "lesson_type": lesson_type,
                "weekday": weekday,
                "start": first.start,
                "end": first.end,
                "date_range_raw": first.date_range_raw,
                "teacher": first.teacher,
                "room": first.room,
                "url_kind": _url_kind(url),
                "raw_text": first.raw_text,
            },
        })
    return meetings, skipped, warnings


def _read_generated(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _index_by_key(meetings: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(m.get("dmami_key")): m for m in meetings if isinstance(m, dict) and m.get("dmami_key")}


def _write_yaml_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _parse_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date() if value else None
    except Exception:
        return None


def _active_today(meeting: Dict[str, Any], today: date) -> bool:
    return is_date_in_range(today, _parse_date(meeting.get("start_date")), _parse_date(meeting.get("end_date")))


def _stats(events: List[WebEvent], meetings: List[Dict[str, Any]], today: date) -> Dict[str, int]:
    return {
        "cards_total": len(events),
        "with_mts_link": sum(1 for e in events if _url_kind(e.href) == "mts_link"),
        "without_link": sum(1 for e in events if not e.href),
        "offline": sum(1 for m in meetings if m.get("meeting_mode") == "offline"),
        "dates_detected": sum(1 for m in meetings if m.get("start_date") and m.get("end_date")),
        "dates_missing": sum(1 for m in meetings if not (m.get("start_date") and m.get("end_date"))),
        "generated_meetings": len(meetings),
        "active_today": sum(1 for m in meetings if _active_today(m, today)),
    }


def _changes_text(old, new, skipped, warnings, stats) -> str:
    old_keys, new_keys = set(old), set(new)
    lines = [
        f"cards total: {stats['cards_total']}",
        f"with MTS Link: {stats['with_mts_link']}",
        f"without link: {stats['without_link']}",
        f"offline: {stats['offline']}",
        f"with date range: {stats['dates_detected']}",
        f"without date range: {stats['dates_missing']}",
        f"generated meetings: {stats['generated_meetings']}",
        f"active today: {stats['active_today']}",
        "",
        f"new keys: {len(new_keys - old_keys)}",
        f"removed keys: {len(old_keys - new_keys)}",
        f"updated urls: {sum(1 for key in old_keys & new_keys if old[key].get('url') != new[key].get('url'))}",
        f"skipped events: {len(skipped)}",
        f"parse warnings: {len(warnings)}",
    ]
    return "\n".join(lines + [""] + [f"! {w}" for w in warnings]) + "\n"


async def sync_dmami_schedule(cfg: Dict[str, Any], logger) -> Dict[str, Any]:
    dmami_cfg = cfg.get("dmami") or {}
    group = (dmami_cfg.get("group") or "").strip()
    if not group:
        raise RuntimeError("dmami.group is not set")
    now = datetime.now().astimezone()
    today = now.date()
    ts = now.strftime("%Y%m%d-%H%M%S")
    page_prefix = f"dmami_page_{ts}"
    events = await fetch_dmami(
        group,
        headless=True,
        chrome=(cfg.get("chromium") or {}).get("executable_path"),
        debug=bool(dmami_cfg.get("debug")),
        debug_prefix=page_prefix,
    )
    meetings, skipped, warnings = build_generated_meetings(events, cfg, today)
    generated_path = get_generated_config_path()
    previous = _read_generated(generated_path)
    old_index, new_index = _index_by_key(previous.get("meetings") or []), _index_by_key(meetings)
    stats = _stats(events, meetings, today)
    payload = {
        "generated_by": "nts_autojoin.dmami",
        "generated_at": now.isoformat(timespec="seconds"),
        "source": {"group": group, "raw_events": len(events), "dates_detected": stats["dates_detected"], "dates_missing": stats["dates_missing"]},
        "meetings": meetings,
    }
    _write_yaml_atomic(generated_path, payload)
    logs_dir = get_logs_dir()
    raw_path = logs_dir / f"dmami_raw_{ts}.yaml"
    generated_log_path = logs_dir / f"dmami_generated_{ts}.yaml"
    changes_path = logs_dir / f"dmami_changes_{ts}.txt"
    warnings_path = logs_dir / f"dmami_parse_warnings_{ts}.txt"
    unmatched_path = logs_dir / f"dmami_unmatched_{ts}.yaml"
    _write_yaml_atomic(raw_path, {"events": [asdict(e) for e in events], "skipped": skipped})
    _write_yaml_atomic(generated_log_path, payload)
    _write_text(changes_path, _changes_text(old_index, new_index, skipped, warnings, stats))
    _write_text(warnings_path, "\n".join(warnings) if warnings else "No parse warnings.\n")
    if skipped:
        _write_yaml_atomic(unmatched_path, {"skipped": skipped})
    page_html_path = logs_dir / f"{page_prefix}.html"
    page_png_path = logs_dir / f"{page_prefix}.png"
    return {
        **stats,
        "raw_events": len(events),
        "new_keys": len(set(new_index) - set(old_index)),
        "removed_keys": len(set(old_index) - set(new_index)),
        "updated_urls": sum(1 for key in set(old_index) & set(new_index) if old_index[key].get("url") != new_index[key].get("url")),
        "skipped": len(skipped),
        "warnings": len(warnings),
        "generated_path": str(generated_path),
        "raw_path": str(raw_path),
        "generated_log_path": str(generated_log_path),
        "changes_path": str(changes_path),
        "warnings_path": str(warnings_path),
        "unmatched_path": str(unmatched_path) if skipped else "",
        "page_html_path": str(page_html_path) if page_html_path.exists() else "",
        "page_png_path": str(page_png_path) if page_png_path.exists() else "",
    }


def format_sync_report(result: Dict[str, Any]) -> str:
    lines = [
        "DMAMI sync finished.",
        f"Cards found: {result['cards_total']}",
        f"With MTS Link: {result['with_mts_link']}",
        f"Without link: {result['without_link']}",
        f"Offline: {result['offline']}",
        f"With date range: {result['dates_detected']}",
        f"Without date range: {result['dates_missing']}",
        f"Meetings generated: {result['generated_meetings']}",
        f"Active today: {result['active_today']}",
        f"New keys: {result['new_keys']}",
        f"Removed keys: {result['removed_keys']}",
        f"Updated URLs: {result['updated_urls']}",
        f"Warnings: {result['warnings']}",
        "",
        f"Generated: {result['generated_path']}",
        f"Raw log: {result['raw_path']}",
        f"Generated log: {result['generated_log_path']}",
        f"Changes: {result['changes_path']}",
        f"Warnings log: {result['warnings_path']}",
    ]
    if result.get("page_html_path"):
        lines.append(f"Page HTML: {result['page_html_path']}")
    if result.get("page_png_path"):
        lines.append(f"Page PNG: {result['page_png_path']}")
    if result.get("unmatched_path"):
        lines.append(f"Unmatched: {result['unmatched_path']}")
    return "\n".join(lines)
