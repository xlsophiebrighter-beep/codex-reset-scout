from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "lookback_hours": 48,
    "timeout_seconds": 15,
    "community_min_reports": 2,
    "sources": {
        "tibo_feed_urls": [
            "https://nitter.net/thsottiaux/rss",
        ],
        "openai_status": True,
        "developer_community": True,
        "github_issues": True,
        "reddit": True,
        "extra_feeds": [],
    },
    "local": {
        "enabled": True,
        "codex_home": None,
        "include_credits": False,
        "credit_expiry_warning_hours": 72,
    },
    "notifications": {
        "webhook_url": None,
    },
}


def default_config_path() -> Path:
    override = os.environ.get("CODEX_SCOUT_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "CodexResetScout" / "config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / (
        "codex-reset-scout/config.json"
    )


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path).expanduser() if path else default_config_path()
    override: dict[str, Any] = {}
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config root must be a JSON object")
        override = data
    config = _merge(DEFAULT_CONFIG, override)
    webhook = os.environ.get("CODEX_SCOUT_WEBHOOK_URL")
    if webhook:
        config["notifications"]["webhook_url"] = webhook
    return config
