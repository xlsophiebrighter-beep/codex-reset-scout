from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def default_state_path() -> Path:
    override = os.environ.get("CODEX_SCOUT_STATE")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "CodexResetScout" / "state.json"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / (
        "codex-reset-scout/state.json"
    )


class StateStore:
    """Small, atomic, token-free seen-state store."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_state_path()
        self.data = self._load()

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "version": 1,
            "updated_at": None,
            "seen_events": {},
            "source_failures": {},
            "local": {},
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.empty()
        return data if isinstance(data, dict) else self.empty()

    def is_seen(self, event_id: str) -> bool:
        return event_id in self.data.setdefault("seen_events", {})

    def mark_seen(self, event_id: str, category: str) -> None:
        seen = self.data.setdefault("seen_events", {})
        seen[event_id] = {
            "category": category,
            "seen_at": datetime.now(UTC).isoformat(),
        }
        if len(seen) > 2000:
            ordered = sorted(seen.items(), key=lambda item: item[1].get("seen_at", ""))
            self.data["seen_events"] = dict(ordered[-2000:])

    def record_source_health(self, source: str, ok: bool) -> int:
        failures = self.data.setdefault("source_failures", {})
        failures[source] = 0 if ok else int(failures.get(source, 0)) + 1
        return int(failures[source])

    def get_local(self) -> dict[str, Any]:
        return dict(self.data.setdefault("local", {}))

    def set_local(self, value: dict[str, Any]) -> None:
        self.data["local"] = value

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(UTC).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        handle, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
