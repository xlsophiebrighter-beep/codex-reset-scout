from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG
from .models import UsageSnapshot, WindowSnapshot
from .state import StateStore

_TOKEN_EVENT_TYPES = frozenset({"token_count", "tokenCount"})


def default_codex_home() -> Path:
    """Return the configured Codex home without inspecting its contents."""

    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser() if override else Path.home() / ".codex"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _pick(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        candidate = value.get(key)
        if candidate is not None:
            return candidate
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
            if not math.isfinite(number):
                return None
            if abs(number) > 10_000_000_000:
                number /= 1000
            parsed = datetime.fromtimestamp(number, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is not None:
            return _parse_datetime(number)
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_percent(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not 0 <= parsed <= 100:
        return None
    return parsed


def _parse_window(rate_limits: Mapping[str, Any], *names: str) -> WindowSnapshot:
    raw: Mapping[str, Any] = {}
    for name in names:
        candidate = rate_limits.get(name)
        if isinstance(candidate, Mapping):
            raw = candidate
            break
    return WindowSnapshot(
        used_percent=_parse_percent(_pick(raw, "used_percent", "usedPercent", "usage_percent")),
        resets_at=_parse_datetime(_pick(raw, "resets_at", "resetsAt", "reset_at", "resetAt")),
    )


def _parse_token_count_line(line: str) -> UsageSnapshot | None:
    if "token_count" not in line and "tokenCount" not in line:
        return None
    try:
        raw = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    event = _mapping(raw)
    payload = _mapping(event.get("payload"))
    event_type = _pick(payload, "type", "event_type", "eventType")
    if event_type not in _TOKEN_EVENT_TYPES:
        return None
    observed_at = _parse_datetime(_pick(event, "timestamp", "created_at", "createdAt"))
    if observed_at is None:
        return None

    info = _mapping(payload.get("info"))
    rate_limits = _mapping(_pick(payload, "rate_limits", "rateLimits"))
    if not rate_limits:
        rate_limits = _mapping(_pick(info, "rate_limits", "rateLimits"))
    plan_type = _pick(rate_limits, "plan_type", "planType")
    return UsageSnapshot(
        observed_at=observed_at,
        plan_type=str(plan_type)[:64] if plan_type is not None else "",
        primary=_parse_window(rate_limits, "primary", "short_window", "shortWindow"),
        secondary=_parse_window(rate_limits, "secondary", "long_window", "longWindow"),
    )


def _jsonl_files(codex_home: Path) -> list[Path]:
    files: list[Path] = []
    for directory_name in ("sessions", "archived_sessions"):
        root = codex_home / directory_name
        if not root.is_dir():
            continue
        try:
            candidates = root.rglob("*.jsonl")
            files.extend(path for path in candidates if path.is_file() and not path.is_symlink())
        except OSError:
            continue
    return files


def latest_usage_snapshot(codex_home: str | Path | None = None) -> UsageSnapshot | None:
    """Read the newest local token-count event and return only quota metadata.

    File names, session identifiers, working directories, and message bodies are never copied
    into the returned model.
    """

    home = Path(codex_home).expanduser() if codex_home else default_codex_home()
    latest: UsageSnapshot | None = None
    for path in _jsonl_files(home):
        try:
            stream = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line in stream:
                snapshot = _parse_token_count_line(line)
                if snapshot is not None and (
                    latest is None or snapshot.observed_at > latest.observed_at
                ):
                    latest = snapshot
    return latest


def configured_usage_snapshot(config: Mapping[str, Any]) -> UsageSnapshot | None:
    """Honor the existing local configuration before reading Codex session metadata."""

    local = _mapping(config.get("local"))
    enabled = local.get("enabled", DEFAULT_CONFIG["local"]["enabled"])
    if enabled is not True:
        return None
    return latest_usage_snapshot(local.get("codex_home"))


def _different(before: Any, after: Any) -> bool:
    if isinstance(before, float) and isinstance(after, float):
        return not math.isclose(before, after, rel_tol=0, abs_tol=1e-9)
    return before != after


def compare_usage(
    previous: UsageSnapshot | None,
    current: UsageSnapshot,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return only changed used-percent and reset-at fields.

    The initial observation is a baseline and therefore produces no change event.
    """

    if previous is None:
        return {}
    changes: dict[str, dict[str, dict[str, Any]]] = {}
    for name in ("primary", "secondary"):
        before_window = getattr(previous, name)
        after_window = getattr(current, name)
        window_changes: dict[str, dict[str, Any]] = {}
        for field_name in ("used_percent", "resets_at"):
            before = getattr(before_window, field_name)
            after = getattr(after_window, field_name)
            if _different(before, after):
                window_changes[field_name] = {"before": before, "after": after}
        if window_changes:
            changes[name] = window_changes
    return changes


def usage_snapshot_to_state(snapshot: UsageSnapshot) -> dict[str, Any]:
    """Create the token-free subset permitted in StateStore.local."""

    def window(value: WindowSnapshot) -> dict[str, Any]:
        return {
            "used_percent": value.used_percent,
            "resets_at": value.resets_at.isoformat() if value.resets_at else None,
        }

    return {
        "observed_at": snapshot.observed_at.isoformat(),
        "plan_type": snapshot.plan_type,
        "primary": window(snapshot.primary),
        "secondary": window(snapshot.secondary),
    }


def usage_snapshot_from_state(value: Any) -> UsageSnapshot | None:
    """Load a prior sanitized snapshot while tolerating missing or renamed backend fields."""

    data = _mapping(value)
    observed_at = _parse_datetime(data.get("observed_at"))
    if observed_at is None:
        return None

    def window(name: str) -> WindowSnapshot:
        raw = _mapping(data.get(name))
        return WindowSnapshot(
            used_percent=_parse_percent(raw.get("used_percent")),
            resets_at=_parse_datetime(raw.get("resets_at")),
        )

    return UsageSnapshot(
        observed_at=observed_at,
        plan_type=str(data.get("plan_type") or "")[:64],
        primary=window("primary"),
        secondary=window("secondary"),
    )


def update_usage_state(
    store: StateStore,
    current: UsageSnapshot,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Compare and stage a sanitized local snapshot without saving automatically."""

    local = store.get_local()
    previous = usage_snapshot_from_state(local.get("usage"))
    changes = compare_usage(previous, current)
    local["usage"] = usage_snapshot_to_state(current)
    store.set_local(local)
    return changes


# A readable alias for callers that prefer a collection-oriented name.
find_latest_usage = latest_usage_snapshot
