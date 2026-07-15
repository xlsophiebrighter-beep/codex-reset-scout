from __future__ import annotations

import json
from datetime import UTC, datetime

from codex_reset_scout.local_usage import (
    compare_usage,
    configured_usage_snapshot,
    latest_usage_snapshot,
    update_usage_state,
    usage_snapshot_from_state,
)
from codex_reset_scout.models import UsageSnapshot, WindowSnapshot
from codex_reset_scout.state import StateStore


def _write_jsonl(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _event(timestamp: str, rate_limits: dict, *, camel_case: bool = False) -> dict:
    if camel_case:
        return {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "tokenCount", "info": {"rateLimits": rate_limits}},
        }
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": rate_limits},
    }


def test_latest_usage_reads_sessions_and_archives_without_identity_leak(tmp_path) -> None:
    secret_path = "C:/private/research-project"
    secret_session = "session-secret-123"
    sessions = tmp_path / "sessions" / "2026" / "active.jsonl"
    archived = tmp_path / "archived_sessions" / "old.jsonl"
    _write_jsonl(
        sessions,
        [
            {
                "timestamp": "2026-07-15T01:00:00Z",
                "payload": {
                    "type": "session_meta",
                    "id": secret_session,
                    "cwd": secret_path,
                },
            },
            {"payload": {"type": "message", "text": "private message body"}},
            _event(
                "2026-07-15T02:00:00Z",
                {
                    "plan_type": "pro",
                    "primary": {"used_percent": 12, "resets_at": 1784084400},
                },
            ),
        ],
    )
    _write_jsonl(
        archived,
        [
            _event(
                "2026-07-15T03:00:00Z",
                {
                    "planType": "prolite",
                    "primary": {
                        "usedPercent": "62",
                        "resetsAt": "2026-07-22T01:20:05Z",
                    },
                    "secondary": {"usedPercent": "bad", "resetsAt": None},
                },
                camel_case=True,
            )
        ],
    )

    snapshot = latest_usage_snapshot(tmp_path)

    assert snapshot is not None
    assert snapshot.observed_at == datetime(2026, 7, 15, 3, tzinfo=UTC)
    assert snapshot.plan_type == "prolite"
    assert snapshot.primary.used_percent == 62
    assert snapshot.primary.resets_at == datetime(2026, 7, 22, 1, 20, 5, tzinfo=UTC)
    assert snapshot.secondary.used_percent is None
    rendered = repr(snapshot)
    assert secret_path not in rendered
    assert secret_session not in rendered
    assert "private message body" not in rendered


def test_latest_usage_tolerates_malformed_lines_and_invalid_fields(tmp_path) -> None:
    path = tmp_path / "sessions" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "not-json\n"
        + json.dumps(
            _event(
                "2026-07-15T04:00:00Z",
                {
                    "plan_type": {"unexpected": "shape"},
                    "primary": {"used_percent": 101, "resets_at": "not-a-date"},
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = latest_usage_snapshot(tmp_path)

    assert snapshot is not None
    assert snapshot.primary == WindowSnapshot()
    assert len(snapshot.plan_type) <= 64


def test_compare_usage_reports_only_changed_quota_fields() -> None:
    reset_before = datetime(2026, 7, 20, tzinfo=UTC)
    reset_after = datetime(2026, 7, 22, tzinfo=UTC)
    previous = UsageSnapshot(
        observed_at=datetime(2026, 7, 15, 1, tzinfo=UTC),
        plan_type="pro",
        primary=WindowSnapshot(67, reset_before),
        secondary=WindowSnapshot(20, reset_before),
    )
    current = UsageSnapshot(
        observed_at=datetime(2026, 7, 15, 2, tzinfo=UTC),
        plan_type="prolite",
        primary=WindowSnapshot(0, reset_after),
        secondary=WindowSnapshot(20, reset_before),
    )

    changes = compare_usage(previous, current)

    assert changes == {
        "primary": {
            "used_percent": {"before": 67, "after": 0},
            "resets_at": {"before": reset_before, "after": reset_after},
        }
    }
    assert compare_usage(None, current) == {}


def test_state_round_trip_is_sanitized_and_preserves_other_local_state(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.set_local({"banked_reset_count": 3})
    current = UsageSnapshot(
        observed_at=datetime(2026, 7, 15, 2, tzinfo=UTC),
        plan_type="pro",
        primary=WindowSnapshot(25, datetime(2026, 7, 20, tzinfo=UTC)),
    )

    assert update_usage_state(store, current) == {}
    local = store.get_local()
    assert local["banked_reset_count"] == 3
    assert set(local["usage"]) == {
        "observed_at",
        "plan_type",
        "primary",
        "secondary",
    }
    assert usage_snapshot_from_state(local["usage"]) == current


def test_config_can_disable_local_file_reads(tmp_path) -> None:
    config = {"local": {"enabled": False, "codex_home": str(tmp_path / "missing")}}
    assert configured_usage_snapshot(config) is None
