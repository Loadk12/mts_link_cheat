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
    p = Path("config/schedule.yaml")
    if not p.exists():
        raise FileNotFoundError(p)
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _save_cfg(cfg: Dict) -> None:
    """
    Атомарное сохранение: пишем во временный файл и переименовываем.
    """
    p = Path("config/schedule.yaml")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(p)


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
    c = _load_cfg()
    meetings = c.get("meetings", []) or []

    changes: List[str] = []
    updated = 0
    matched_ids: set[int] = set()

    for m in meetings:
        name = m.get("name", "")
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
    Path("logs").mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    raw_path = Path("logs") / f"dmami_raw_{ts}.yaml"
    map_path = Path("logs") / f"dmami_changes_{ts}.txt"
    unmatched_path = Path("logs") / f"dmami_unmatched_{ts}.yaml"

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
