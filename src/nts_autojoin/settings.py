import yaml, os
from typing import Any, Dict
def load_config() -> Dict[str, Any]:
    base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    cfg_path = os.path.join(base, "config", "schedule.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
