# -*- coding: utf-8 -*-
"""
Связка: тянем ссылки с rasp.dmami.ru и обновляем config/schedule.yaml
по совпадению имён встреч (нечувствительно к регистру, с частичным совпадением),
а также подсвечиваем новые/нематченные пары в отдельных логах.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple
import re

import yaml

from .dmami_scraper import fetch_dmami, WebEvent
from .notifier import notify, send_document
from .settings import get_config_path, get_logs_dir, save_config


def _norm(s: str) -> str:
    """
    Нормализация названий для сопоставления:
    - lower/strip
    - заменяем ё->е
    - схлопываем пробелы
    """
    s = (s or "").strip().lower()
    s = s.replace("ё", "е")
    s = re.sub(r"\s+", " ", s)
    return s


def _build_title_map(events: List[WebEvent]) -> Dict[str, WebEvent]:
    """
    Строим словарь нормализованное_название -> WebEvent.
    При дублях оставляем первый.
    """
    m: Dict[str, WebEvent] = {}
    for e in events:
        key = _norm(e.title)
        if not key:
            continue
        m.setdefault(key, e)
    return m


DAY_ALIASES = {
    "mon": ("mon", "monday", "пн", "понедельник"),
    "tue": ("tue", "tuesday", "вт", "вторник"),
    "wed": ("wed", "wednesday", "ср", "среда"),
    "thu": ("thu", "thursday", "чт", "четверг"),
    "fri": ("fri", "friday", "пт", "пятница"),
    "sat": ("sat", "saturday", "сб", "суббота"),
    "sun": ("sun", "sunday", "вс", "воскресенье"),
}


def _cron_day(cron_expr: str) -> str:
    parts = (cron_expr or "").split()
    return _norm(parts[4]) if len(parts) >= 5 else ""


def _cron_start(cron_expr: str) -> str:
    parts = (cron_expr or "").split()
    if len(parts) < 2:
        return ""
    try:
        return f"{int(parts[1]):02d}:{int(parts[0]):02d}"
    except Exception:
        return ""


def _event_day_key(day: str) -> str:
    value = _norm(day)
    for key, aliases in DAY_ALIASES.items():
        if any(alias in value for alias in aliases):
            return key
    return value


def _meeting_day_key(cron_expr: str) -> str:
    day = _cron_day(cron_expr)
    for key, aliases in DAY_ALIASES.items():
        if day in aliases:
            return key
    return day


def _event_keys(event: WebEvent) -> List[Tuple[str, str, str]]:
    return [(_norm(event.title), _event_day_key(event.day), event.start)]


def _meeting_keys(meeting: Dict) -> List[Tuple[str, str, str]]:
    cron = meeting.get("cron", "")
    day = _meeting_day_key(cron)
    start = _cron_start(cron)
    names = [meeting.get("name", "")]
    aliases = meeting.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    names.extend(aliases)
    return [(_norm(name), day, start) for name in names if _norm(name)]


def _match_event(title: str, title_map: Dict[str, WebEvent]) -> WebEvent | None:
    """
    Пытаемся найти событие по названию встречи из config:
    1) точное совпадение нормализованной строки
    2) если нет — частичное (ключ является подстрокой другого).
    """
    key = _norm(title)
    if not key:
        return None

    if key in title_map:
        return title_map[key]

    # частичное совпадение: либо meeting содержит dmami, либо наоборот
    candidates: List[Tuple[str, WebEvent]] = []
    for k, ev in title_map.items():
        if key in k or k in key:
            candidates.append((k, ev))

    if len(candidates) == 1:
        return candidates[0][1]

    # если несколько кандидатов — не гадаем, лучше явно настроить имя
    return None


def _load_cfg() -> Dict:
    p = get_config_path()
    if not p.exists():
        raise FileNotFoundError(p)
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _save_cfg(cfg: Dict) -> None:
    """
    Атомарное сохранение: пишем во временный файл и переименовываем.
    """
    save_config(cfg)


async def scrape_and_update(cfg: Dict, logger) -> Tuple[int, List[str]]:
    """
    Высокоуровневая функция:
    - скачивает события с DMAMI для dmami.group;
    - обновляет url в meetings при совпадении по имени;
    - логирует новые/нематченные пары.

    Возвращает: (сколько url обновили, список строк лога для человекочитаемого отчёта).
    """
    group = ((cfg.get("dmami") or {}).get("group") or "").strip()
    if not group:
        raise RuntimeError("В конфиге нет dmami.group")

    logger.info(f"[dmami] скрейп для группы {group}…")
    events = await fetch_dmami(
        group,
        headless=True,
        chrome=(cfg.get("chromium") or {}).get("executable_path"),
    )

    title_map = _build_title_map(events)
    composite_map: Dict[Tuple[str, str, str], WebEvent] = {}
    duplicate_keys: set[Tuple[str, str, str]] = set()
    for event in events:
        for key in _event_keys(event):
            if key in composite_map:
                duplicate_keys.add(key)
            else:
                composite_map[key] = event
    for key in duplicate_keys:
        composite_map.pop(key, None)

    c = _load_cfg()
    meetings = c.get("meetings", []) or []

    changes: List[str] = []
    updated = 0
    matched_ids: set[int] = set()

    for m in meetings:
        name = m.get("name", "")
        ev = None
        for key in _meeting_keys(m):
            ev = composite_map.get(key)
            if ev:
                break
        if ev is None:
            ev = _match_event(name, title_map)
        if not ev:
            continue

        old = (m.get("url") or "").strip()
        new = (ev.href or "").strip()
        if old != new and new:
            m["url"] = new
            updated += 1
            changes.append(f"• {name}\n    {old or '—'}\n →  {new}")
        matched_ids.add(id(ev))

    unmatched: List[WebEvent] = [e for e in events if id(e) not in matched_ids]

    if updated > 0:
        _save_cfg(c)
        logger.info(f"[dmami] обновлено ссылок: {updated}")
    else:
        logger.info("[dmami] совпадений для обновления не нашли")

    # отчёты в файлы
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    raw_path = logs_dir / f"dmami_raw_{ts}.yaml"
    map_path = logs_dir / f"dmami_changes_{ts}.txt"
    unmatched_path = logs_dir / f"dmami_unmatched_{ts}.yaml"

    import yaml as _y

    raw_path.write_text(
        _y.safe_dump(
            [e.__dict__ for e in events],
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    map_body_lines: List[str] = list(changes)

    if unmatched:
        map_body_lines.append("")
        map_body_lines.append(
            "Новые/нематченные пары (нет соответствующей встречи в config/schedule.yaml):"
        )
        for e in unmatched:
            map_body_lines.append(
                f"! {e.title} — {e.day} {e.start}-{e.end}\n    {e.href}"
            )
        unmatched_path.write_text(
            _y.safe_dump(
                [e.__dict__ for e in unmatched],
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    map_path.write_text(
        "\n".join(map_body_lines) if map_body_lines else "Нет изменений.",
        encoding="utf-8",
    )

    # уведомления
    try:
        msg = (
            f"📥 DMAMI: найдено {len(events)} ссылок. "
            f"Обновлено в расписании: {updated}. "
            f"Новых/нематченных: {len(unmatched)}."
        )
        await notify(cfg, msg)
        await send_document(cfg, str(raw_path), caption="dmami_raw.yaml")
        await send_document(cfg, str(map_path), caption="dmami_changes.txt")
        if unmatched:
            await send_document(
                cfg, str(unmatched_path), caption="dmami_unmatched.yaml"
            )
    except Exception:
        # уведомления не критичны
        pass

    return updated, changes
