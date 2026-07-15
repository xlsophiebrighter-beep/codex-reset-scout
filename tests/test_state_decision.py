from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codex_reset_scout.decision import recommend
from codex_reset_scout.models import Alert, ResetCredit, UsageSnapshot, WindowSnapshot
from codex_reset_scout.state import StateStore


def decision_alert(category: str, *, stage: str = "announcement") -> Alert:
    return Alert(
        event_id=f"{stage}:{category}",
        category=category,
        stage=stage,
        confidence="medium" if stage == "community_signal" else "confirmed",
        source="community" if stage == "community_signal" else "tibo",
        title=category,
        reason="test signal",
    )


def test_state_round_trip_is_token_free(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.mark_seen("tibo-123", "pre_reset_hint")
    store.set_local({"primary_used_percent": 42.0})
    store.save()

    reloaded = StateStore(path)
    assert reloaded.is_seen("tibo-123")
    assert reloaded.get_local()["primary_used_percent"] == 42.0
    assert "token" not in path.read_text(encoding="utf-8").lower()


def test_confirmed_reset_recommends_spending_remaining_quota():
    alert = Alert(
        event_id="official-1",
        category="pre_reset_confirmed",
        stage="announcement",
        confidence="confirmed",
        source="tibo",
        title="Another reset lands in 30 minutes",
        reason="explicit future reset",
    )

    decision = recommend([alert], None, [])

    assert decision.action == "spend_remaining_quota"
    assert decision.urgency == "high"


def test_expiring_credit_beats_normal_usage_advice():
    now = datetime(2026, 7, 15, tzinfo=UTC)
    usage = UsageSnapshot(
        observed_at=now,
        primary=WindowSnapshot(used_percent=50.0),
    )
    credit = ResetCredit(
        status="available",
        expires_at=now + timedelta(hours=24),
    )

    decision = recommend([], usage, [credit], now=now)

    assert decision.action == "use_expiring_credit"


def test_community_reset_is_not_treated_as_official_or_high_urgency():
    decision = recommend(
        [decision_alert("pre_reset_confirmed", stage="community_signal")],
        None,
        [],
    )

    assert decision.action == "verify_and_prepare_quota"
    assert decision.urgency == "medium"
    assert "official reset" not in decision.reason.casefold()


def test_official_credit_and_policy_signals_have_top_level_decisions():
    expected = {
        "pre_credit": "wait_for_credit",
        "credit_granted": "check_local_credit",
        "policy_change": "review_usage_plan",
    }

    for category, action in expected.items():
        decision = recommend([decision_alert(category)], None, [])
        assert decision.action == action
        assert decision.urgency == "medium"


def test_community_credit_signal_is_downgraded_for_verification():
    decision = recommend(
        [decision_alert("pre_credit", stage="community_signal")],
        None,
        [],
    )

    assert decision.action == "verify_credit_signal"
    assert decision.urgency == "low"


def test_credit_expiry_threshold_is_configurable():
    now = datetime(2026, 7, 15, tzinfo=UTC)
    credit = ResetCredit(
        status="available",
        expires_at=now + timedelta(hours=48),
    )

    outside_threshold = recommend(
        [],
        None,
        [credit],
        now=now,
        credit_expiry_warning_hours=24,
    )
    inside_threshold = recommend(
        [],
        None,
        [credit],
        now=now,
        credit_expiry_warning_hours=48,
    )

    assert outside_threshold.action == "continue_normally"
    assert inside_threshold.action == "use_expiring_credit"
    assert "48 hours" in inside_threshold.reason
