from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codex_reset_scout.classify import classify_item, classify_items
from codex_reset_scout.models import SourceItem

NOW = datetime(2026, 7, 15, 3, 30, tzinfo=UTC)


def item(
    text: str,
    *,
    event_id: str = "event-1",
    source: str = "tibo",
    trust: str = "tibo",
    published_at: datetime | None = NOW,
) -> SourceItem:
    return SourceItem(
        event_id=event_id,
        source=source,
        trust=trust,
        title=text,
        url=f"https://example.test/{event_id}",
        published_at=published_at,
    )


def test_explicit_pre_reset_from_tibo_is_actionable() -> None:
    alert = classify_item(item("Codex usage limits will be fully reset again in the next hour."))

    assert alert is not None
    assert alert.category == "pre_reset_confirmed"
    assert alert.stage == "pre_warning"
    assert alert.confidence == "confirmed"
    assert alert.recommended_action == "spend_remaining_quota"


def test_explicit_no_reset_suppresses_false_positive() -> None:
    assert classify_item(item("There will be no reset today, just a normal update.")) is None
    assert classify_item(item("There is no Codex reset planned for today.")) is None
    assert classify_item(item("We have no plans to reset Codex limits tomorrow.")) is None
    assert classify_item(item("This is not a reset. The old window remains.")) is None
    assert (
        classify_item(
            item(
                "Thinking I am about to announce a reset. But no. "
                "I'm just looking for feedback."
            )
        )
        is None
    )


def test_milestone_hint_is_probable_pre_warning() -> None:
    alert = classify_item(item("Tomorrow might be 9M active users celebration day. Just saying."))

    assert alert is not None
    assert alert.category == "pre_reset_hint"
    assert alert.confidence == "probable"


def test_future_reset_credit_grant_is_detected() -> None:
    alert = classify_item(item("Tomorrow we'll gift everyone two banked reset credits."))

    assert alert is not None
    assert alert.category == "pre_credit"
    assert alert.recommended_action == "wait_for_credit"


def test_already_granted_banked_reset_is_not_a_future_credit_warning() -> None:
    alert = classify_item(
        item(
            "We have added a banked reset to everyone's account. "
            "You can apply the reset in the desktop app."
        )
    )

    assert alert is not None
    assert alert.category == "credit_granted"
    assert alert.stage == "announcement"
    assert alert.recommended_action == "check_local_credit"


def test_reset_currently_rolling_out_is_confirmed() -> None:
    alert = classify_item(
        item("We are once again resetting the usage limits for all Codex users.")
    )

    assert alert is not None
    assert alert.category == "pre_reset_confirmed"


def test_material_limit_policy_change_is_detected() -> None:
    alert = classify_item(item("We have removed the 5-hour limit for Codex users."))

    assert alert is not None
    assert alert.category == "policy_change"
    assert alert.recommended_action == "review_usage_plan"


def test_no_reset_does_not_hide_an_independent_policy_change() -> None:
    alert = classify_item(
        item("No reset today, but we have removed the 5-hour limit for Codex users.")
    )

    assert alert is not None
    assert alert.category == "policy_change"


def test_single_community_report_is_not_actionable() -> None:
    report = item(
        "Codex will reset later today according to the banner.",
        source="reddit",
        trust="community",
    )

    assert classify_item(report) is None
    assert classify_items([report], min_reports=2) == []


def test_github_maintainer_issue_is_still_community_evidence() -> None:
    report = item(
        "Codex usage limits will reset in the next hour.",
        source="github_issues",
        trust="maintainer",
    )

    assert classify_item(report) is None
    assert classify_items([report], min_reports=2) == []


def test_two_distinct_community_reports_form_one_actionable_signal() -> None:
    reports = [
        item(
            "Codex will reset later today according to the banner.",
            event_id="reddit-1",
            source="reddit",
            trust="community",
        ),
        item(
            "A Codex usage reset is scheduled in the next hour.",
            event_id="forum-1",
            source="developer_community",
            trust="community",
        ),
    ]

    alerts = classify_items(reports, min_reports=2)

    assert len(alerts) == 1
    assert alerts[0].category == "pre_reset_confirmed"
    assert alerts[0].stage == "community_signal"
    assert alerts[0].confidence == "medium"
    assert "not an official announcement" in alerts[0].reason
    assert alerts[0].recommended_action == "verify_and_prepare_quota"


def test_duplicate_community_event_ids_do_not_satisfy_threshold() -> None:
    report = item(
        "Codex will reset later today.",
        event_id="same-report",
        source="reddit",
        trust="community",
    )

    assert classify_items([report, report], min_reports=2) == []


def test_two_reports_from_the_same_community_source_do_not_corroborate() -> None:
    reports = [
        item(
            "Codex will reset later today.",
            event_id="reddit-1",
            source="reddit",
            trust="community",
        ),
        item(
            "A Codex reset is scheduled in the next hour.",
            event_id="reddit-2",
            source="reddit",
            trust="community",
            published_at=NOW + timedelta(hours=1),
        ),
    ]

    assert classify_items(reports, min_reports=2) == []


def test_community_reports_require_dates_and_a_six_hour_window() -> None:
    missing_date = item(
        "Codex will reset later today.",
        event_id="reddit-undated",
        source="reddit",
        trust="community",
        published_at=None,
    )
    dated = item(
        "A Codex reset is scheduled in the next hour.",
        event_id="forum-dated",
        source="developer_community",
        trust="community",
    )
    too_late = item(
        "A Codex reset is scheduled later today.",
        event_id="github-late",
        source="github_issues",
        trust="community",
        published_at=NOW + timedelta(hours=6, seconds=1),
    )

    assert classify_items([missing_date, dated], min_reports=2) == []
    assert classify_items([dated, too_late], min_reports=2) == []


def test_community_milestones_are_clustered_by_milestone_number() -> None:
    reports = [
        item(
            "Tomorrow might be 8M active users celebration day. Just saying.",
            event_id="reddit-8m",
            source="reddit",
            trust="community",
        ),
        item(
            "The 8 million active users milestone celebration may be tomorrow.",
            event_id="forum-8m",
            source="developer_community",
            trust="community",
            published_at=NOW + timedelta(minutes=30),
        ),
        item(
            "Tomorrow might be 9M active users celebration day. Stay tuned.",
            event_id="github-9m",
            source="github_issues",
            trust="community",
        ),
        item(
            "The 9 million active users milestone celebration could be soon.",
            event_id="feed-9m",
            source="extra_feed",
            trust="community",
            published_at=NOW + timedelta(minutes=45),
        ),
    ]

    alerts = classify_items(reports, min_reports=2)

    assert len(alerts) == 2
    assert {alert.category for alert in alerts} == {"pre_reset_hint"}
