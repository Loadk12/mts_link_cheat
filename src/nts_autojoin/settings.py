from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml


def get_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_config_path() -> Path:
    return get_project_root() / "config" / "schedule.yaml"


def get_local_config_path() -> Path:
    return get_project_root() / "config" / "config.local.yaml"


def get_generated_config_path() -> Path:
    return get_project_root() / "config" / "schedule.generated.yaml"


def get_overrides_config_path() -> Path:
    return get_project_root() / "config" / "schedule.overrides.yaml"


def get_logs_dir() -> Path:
    return get_project_root() / "logs"


def get_secrets_dir() -> Path:
    return get_project_root() / "secrets"


def resolve_project_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return get_project_root() / path


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _merge_config_layers(
    base: Dict[str, Any],
    generated: Dict[str, Any],
    overrides: Dict[str, Any],
    local: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = dict(base)
    dmami = cfg.get("dmami") or {}
    generated_enabled = bool(dmami.get("auto_generate_schedule"))

    generated_meetings = generated.get("meetings") if generated else None
    if generated_enabled and isinstance(generated_meetings, list):
        cfg["meetings"] = generated_meetings
        cfg["generated_schedule"] = {
            "path": str(get_generated_config_path()),
            "exists": True,
            "enabled": True,
            "generated_at": generated.get("generated_at"),
            "source": generated.get("source") or {},
        }
    elif generated_enabled:
        cfg["meetings"] = []
        cfg["generated_schedule"] = {
            "path": str(get_generated_config_path()),
            "exists": False,
            "enabled": True,
            "generated_at": None,
            "source": {},
        }
    else:
        cfg["generated_schedule"] = {
            "path": str(get_generated_config_path()),
            "exists": bool(generated),
            "enabled": generated_enabled,
            "generated_at": generated.get("generated_at") if generated else None,
            "source": generated.get("source") if generated else {},
        }

    if overrides:
        cfg = _apply_meeting_overrides(cfg, overrides)

    cfg = _deep_merge(cfg, local)
    return _apply_default_join(cfg)


def _meeting_override_keys(meeting: Dict[str, Any]) -> set[str]:
    keys = set()
    for key in ("dmami_key", "name"):
        value = meeting.get(key)
        if value:
            keys.add(str(value).strip().lower())
    aliases = meeting.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    for alias in aliases:
        if alias:
            keys.add(str(alias).strip().lower())
    return keys


def _apply_meeting_overrides(
    cfg: Dict[str, Any], overrides: Dict[str, Any]
) -> Dict[str, Any]:
    override_items = overrides.get("meetings") or []
    if not isinstance(override_items, list):
        return cfg

    override_map: Dict[str, Dict[str, Any]] = {}
    for item in override_items:
        if not isinstance(item, dict):
            continue
        for key in _meeting_override_keys(item):
            override_map[key] = item

    if not override_map:
        return cfg

    meetings = []
    for meeting in cfg.get("meetings", []) or []:
        if not isinstance(meeting, dict):
            meetings.append(meeting)
            continue
        override = None
        for key in _meeting_override_keys(meeting):
            override = override_map.get(key)
            if override:
                break
        if not override:
            meetings.append(meeting)
            continue
        merged = _deep_merge(meeting, {k: v for k, v in override.items() if k != "aliases"})
        if merged.get("disabled"):
            continue
        meetings.append(merged)

    cfg = dict(cfg)
    cfg["meetings"] = meetings
    return cfg


def _apply_default_join(cfg: Dict[str, Any]) -> Dict[str, Any]:
    default_join = cfg.get("default_join")
    if not default_join:
        return cfg

    changed = False
    meetings = []
    for meeting in cfg.get("meetings", []) or []:
        if not isinstance(meeting, dict):
            meetings.append(meeting)
            continue
        if meeting.get("join"):
            meetings.append(meeting)
            continue
        updated = dict(meeting)
        updated["join"] = default_join
        meetings.append(updated)
        changed = True

    if not changed:
        return cfg
    cfg = dict(cfg)
    cfg["meetings"] = meetings
    return cfg


def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    notify = dict(cfg.get("notify") or {})
    token = os.getenv("NTS_TELEGRAM_BOT_TOKEN")
    chat = os.getenv("NTS_TELEGRAM_CHAT_ID")
    allowed = os.getenv("NTS_ALLOWED_USER_IDS")
    if token:
        notify["telegram_bot_token"] = token
    if chat:
        notify["telegram_chat_id"] = chat
    if allowed:
        notify["allowed_user_ids"] = [
            int(x.strip()) for x in allowed.split(",") if x.strip()
        ]
    if notify:
        cfg = dict(cfg)
        cfg["notify"] = notify
    return cfg


def _normalize_chromium_paths(cfg: Dict[str, Any]) -> Dict[str, Any]:
    chromium = dict(cfg.get("chromium") or {})
    extra_args = []
    for arg in chromium.get("extra_args") or []:
        if not isinstance(arg, str) or "=" not in arg:
            extra_args.append(arg)
            continue

        key, value = arg.split("=", 1)
        if key in {
            "--use-file-for-fake-video-capture",
            "--use-file-for-fake-audio-capture",
        }:
            extra_args.append(f"{key}={resolve_project_path(value)}")
        else:
            extra_args.append(arg)

    if extra_args:
        chromium["extra_args"] = extra_args
        cfg = dict(cfg)
        cfg["chromium"] = chromium
    return cfg


def load_config() -> Dict[str, Any]:
    cfg = _merge_config_layers(
        _read_yaml(get_config_path()),
        _read_yaml(get_generated_config_path()),
        _read_yaml(get_overrides_config_path()),
        _read_yaml(get_local_config_path()),
    )
    cfg = _apply_env_overrides(cfg)
    return _normalize_chromium_paths(cfg)


def save_config(cfg: Dict[str, Any]) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(path)
