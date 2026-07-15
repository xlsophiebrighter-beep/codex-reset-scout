from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codex_reset_scout.engine import run_check
from codex_reset_scout.models import (
    ResetCredit,
    SourceHealth,
    SourceItem,
    UsageSnapshot,
    WindowSnapshot,
)

NOW = datetime(2026, 7, 15, 4, tzinfo=UTC)


def config(*, local: bool = False, credits: bool = False) -> dict:
    return {
        "timeout_seconds": 3,
        "community_min_reports": 2,
        "sources": {},
        "local": {
            "enabled": local,
            "codex_home": None,
            "include_credits": credits,
            "credit_expiry_warning_hours": 72,
        },
        "notifications": {"webhook_url": None},
    }


def test_public_alert_is_stage_deduplicated_in_durable_state(tmp_path) -> None:
    item = SourceItem(
        event_id="tibo:123",
        source="tibo",
        trust="tibo",
        title="Codex usage limits will reset in the next hour",
        url="https://x.com/thsottiaux/status/123",
        published_at=NOW,
    )

    def collect(_config):
        return [item], [SourceHealth("tibo", True, NOW, item_count=1)]

    state = tmp_path / "state.json"
    first = run_check(config(), state_path=state, collect=collect, now=NOW)
    second = run_check(config(), state_path=state, collect=collect, now=NOW)

    assert len(first["new_alerts"]) == 1
    assert first["decision"]["action"] == "spend_remaining_quota"
    assert second["new_alerts"] == []
    stored = state.read_text(encoding="utf-8")
    assert "Bearer" not in stored
    assert "access_token" not in stored


def test_failed_webhook_is_retried_before_event_is_marked_seen(tmp_path) -> None:
    item = SourceItem(
        event_id="tibo:retry",
        source="tibo",
        trust="tibo",
        title="Codex usage limits will reset in the next hour",
        published_at=NOW,
    )

    def collect(_config):
        return [item], []

    attempts = []

    def failing_webhook(*_args, **_kwargs):
        attempts.append("failed")
        raise OSError("delivery unavailable")

    def successful_webhook(*_args, **_kwargs):
        attempts.append("delivered")

    configured = config()
    configured["notifications"]["webhook_url"] = "https://example.test/hook"
    state = tmp_path / "state.json"
    first = run_check(
        configured,
        state_path=state,
        collect=collect,
        webhook_sender=failing_webhook,
        now=NOW,
    )
    second = run_check(
        configured,
        state_path=state,
        collect=collect,
        webhook_sender=successful_webhook,
        now=NOW,
    )
    third = run_check(
        configured,
        state_path=state,
        collect=collect,
        webhook_sender=successful_webhook,
        now=NOW,
    )

    assert len(first["new_alerts"]) == 1
    assert first["errors"] == ["webhook delivery failed (OSError)"]
    assert len(second["new_alerts"]) == 1
    assert third["new_alerts"] == []
    assert attempts == ["failed", "delivered"]


def test_two_consecutive_source_failures_alert_once(tmp_path) -> None:
    def unavailable(_config):
        return [], [SourceHealth("tibo-feed", False, NOW, "unavailable")]

    state = tmp_path / "state.json"
    first = run_check(config(), state_path=state, collect=unavailable, now=NOW)
    second = run_check(config(), state_path=state, collect=unavailable, now=NOW)
    third = run_check(config(), state_path=state, collect=unavailable, now=NOW)

    assert first["new_alerts"] == []
    assert second["new_alerts"][0]["category"] == "source_health"
    assert third["new_alerts"] == []


def test_source_health_alert_retries_after_webhook_failure(tmp_path) -> None:
    def unavailable(_config):
        return [], [SourceHealth("tibo-feed", False, NOW, "unavailable")]

    attempts = []

    def failing_webhook(*_args, **_kwargs):
        attempts.append("failed")
        raise OSError("delivery unavailable")

    def successful_webhook(*_args, **_kwargs):
        attempts.append("delivered")

    configured = config()
    configured["notifications"]["webhook_url"] = "https://example.test/hook"
    state = tmp_path / "state.json"

    first = run_check(configured, state_path=state, collect=unavailable, now=NOW)
    second = run_check(
        configured,
        state_path=state,
        collect=unavailable,
        webhook_sender=failing_webhook,
        now=NOW,
    )
    third = run_check(
        configured,
        state_path=state,
        collect=unavailable,
        webhook_sender=successful_webhook,
        now=NOW,
    )
    fourth = run_check(
        configured,
        state_path=state,
        collect=unavailable,
        webhook_sender=successful_webhook,
        now=NOW,
    )

    assert first["new_alerts"] == []
    assert second["new_alerts"][0]["category"] == "source_health"
    assert third["new_alerts"][0]["category"] == "source_health"
    assert fourth["new_alerts"] == []
    assert attempts == ["failed", "delivered"]


def test_old_announcement_expires_and_supersedes_older_hint(tmp_path) -> None:
    hint = SourceItem(
        event_id="tibo:hint",
        source="tibo",
        trust="tibo",
        title="Tomorrow might be 8M active users celebration day. Just saying.",
        published_at=NOW - timedelta(hours=20),
    )
    announced = SourceItem(
        event_id="tibo:announced",
        source="tibo",
        trust="tibo",
        title="We are once again resetting the usage limits for all Codex users.",
        published_at=NOW - timedelta(hours=8),
    )

    def collect(_config):
        return [hint, announced], []

    report = run_check(
        config(),
        state_path=tmp_path / "state.json",
        collect=collect,
        now=NOW,
    )

    assert len(report["alerts"]) == 2
    assert report["new_alerts"] == []
    assert report["decision"]["action"] == "continue_normally"


def test_local_baseline_then_usage_rebase_creates_arrival_alert(tmp_path) -> None:
    before = UsageSnapshot(
        observed_at=NOW,
        plan_type="pro",
        primary=WindowSnapshot(67, NOW + timedelta(days=1)),
        secondary=WindowSnapshot(33, NOW + timedelta(days=7)),
    )
    after = UsageSnapshot(
        observed_at=NOW + timedelta(hours=1),
        plan_type="pro",
        primary=WindowSnapshot(4, NOW + timedelta(days=1, hours=1)),
        secondary=WindowSnapshot(0, NOW + timedelta(days=7, hours=1)),
    )
    snapshots = iter([before, after])

    def usage_reader(**_kwargs):
        return next(snapshots)

    def no_public(_config):
        return [], []

    state = tmp_path / "state.json"
    baseline = run_check(
        config(local=True),
        state_path=state,
        collect=no_public,
        usage_reader=usage_reader,
        now=NOW,
    )
    arrival = run_check(
        config(local=True),
        state_path=state,
        collect=no_public,
        usage_reader=usage_reader,
        now=NOW + timedelta(hours=1),
    )

    assert baseline["new_alerts"] == []
    assert {alert["stage"] for alert in arrival["new_alerts"]} == {"local_arrival"}
    assert arrival["local"]["changes"]["secondary"]["used_percent"] == {
        "before": 33,
        "after": 0,
    }
    assert arrival["local"]["estimated_unused_before_early_rebase_percent"] == {
        "primary": 33.0,
        "secondary": 67.0,
    }


def test_scheduled_rollover_is_not_reported_as_a_hard_reset(tmp_path) -> None:
    before = UsageSnapshot(
        observed_at=NOW,
        primary=WindowSnapshot(80, NOW + timedelta(minutes=30)),
    )
    after = UsageSnapshot(
        observed_at=NOW + timedelta(hours=1),
        primary=WindowSnapshot(0, NOW + timedelta(hours=6)),
    )
    snapshots = iter([before, after])

    def no_public(_config):
        return [], []

    state = tmp_path / "state.json"
    run_check(
        config(local=True),
        state_path=state,
        collect=no_public,
        usage_reader=lambda **_kwargs: next(snapshots),
        now=NOW,
    )
    rollover = run_check(
        config(local=True),
        state_path=state,
        collect=no_public,
        usage_reader=lambda **_kwargs: next(snapshots),
        now=NOW + timedelta(hours=1),
    )

    assert rollover["new_alerts"] == []
    assert rollover["local"]["estimated_unused_before_early_rebase_percent"] == {}


def test_official_signal_does_not_turn_scheduled_rollover_into_hard_reset(tmp_path) -> None:
    before = UsageSnapshot(
        observed_at=NOW,
        primary=WindowSnapshot(80, NOW + timedelta(minutes=30)),
    )
    after = UsageSnapshot(
        observed_at=NOW + timedelta(hours=1),
        primary=WindowSnapshot(0, NOW + timedelta(hours=6)),
    )
    snapshots = iter([before, after])
    official = SourceItem(
        event_id="tibo:scheduled-rollover",
        source="tibo",
        trust="tibo",
        title="Codex usage limits are being reset for all users.",
        published_at=NOW + timedelta(minutes=45),
    )

    def no_public(_config):
        return [], []

    def official_public(_config):
        return [official], [SourceHealth("tibo", True, NOW, item_count=1)]

    state = tmp_path / "state.json"
    run_check(
        config(local=True),
        state_path=state,
        collect=no_public,
        usage_reader=lambda **_kwargs: next(snapshots),
        now=NOW,
    )
    report = run_check(
        config(local=True),
        state_path=state,
        collect=official_public,
        usage_reader=lambda **_kwargs: next(snapshots),
        now=NOW + timedelta(hours=1),
    )

    assert not any(alert["stage"] == "local_arrival" for alert in report["alerts"])
    assert report["local"]["estimated_unused_before_early_rebase_percent"] == {}


def test_undated_public_item_cannot_drive_a_quota_decision(tmp_path) -> None:
    undated = SourceItem(
        event_id="tibo:undated",
        source="tibo",
        trust="tibo",
        title="Codex usage limits will reset in the next hour",
        published_at=None,
    )

    def collect(_config):
        return [undated], []

    report = run_check(
        config(),
        state_path=tmp_path / "state.json",
        collect=collect,
        now=NOW,
    )

    assert len(report["alerts"]) == 1
    assert report["new_alerts"] == []
    assert report["decision"]["action"] == "continue_normally"


def test_credit_reader_is_opt_in_and_expiry_changes_decision(tmp_path) -> None:
    def no_public(_config):
        return [], []

    def should_not_run(**_kwargs):
        raise AssertionError("credit reader ran without opt-in")

    run_check(
        config(local=True, credits=False),
        state_path=tmp_path / "disabled.json",
        collect=no_public,
        usage_reader=lambda **_kwargs: None,
        credit_reader=should_not_run,
        now=NOW,
    )

    def expiring(**kwargs):
        assert kwargs["enabled"] is True
        return [
            ResetCredit(
                status="available",
                reset_type="weekly",
                expires_at=NOW + timedelta(hours=24),
            )
        ]

    report = run_check(
        config(local=True, credits=True),
        state_path=tmp_path / "enabled.json",
        collect=no_public,
        usage_reader=lambda **_kwargs: None,
        credit_reader=expiring,
        now=NOW,
    )

    assert report["local"]["credit_summary"]["available_count"] == 1
    assert report["decision"]["action"] == "use_expiring_credit"
    assert report["new_alerts"][0]["category"] == "credit_expiry"
