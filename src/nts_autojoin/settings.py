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
    cfg = _read_yaml(get_config_path())
    cfg = _deep_merge(cfg, _read_yaml(get_local_config_path()))
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
