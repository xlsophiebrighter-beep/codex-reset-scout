from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .models import Alert, Decision, ResetCredit, UsageSnapshot


def recommend(
    alerts: list[Alert],
    usage: UsageSnapshot | None,
    credits: list[ResetCredit],
    now: datetime | None = None,
    credit_expiry_warning_hours: int = 72,
) -> Decision:
    """Return advice without ever redeeming a credit or changing account state."""

    now = now or datetime.now(UTC)
    official_categories = {
        alert.category for alert in alerts if alert.stage != "community_signal"
    }
    community_categories = {
        alert.category for alert in alerts if alert.stage == "community_signal"
    }
    if "pre_reset_confirmed" in official_categories:
        return Decision(
            action="spend_remaining_quota",
            urgency="high",
            reason="An official reset is imminent; unused allowance may be overwritten.",
        )
    warning_hours = max(0, int(credit_expiry_warning_hours))
    expiring = [
        credit
        for credit in credits
        if credit.status.lower() == "available"
        and credit.expires_at is not None
        and credit.expires_at <= now + timedelta(hours=warning_hours)
    ]
    if expiring:
        return Decision(
            action="use_expiring_credit",
            urgency="high",
            reason=(
                "At least one banked reset credit expires within "
                f"{warning_hours} hours."
            ),
        )
    if "pre_reset_confirmed" in community_categories:
        return Decision(
            action="verify_and_prepare_quota",
            urgency="medium",
            reason=(
                "Corroborated community reports suggest a reset may be coming; "
                "verify the signal and prepare useful quota-consuming work."
            ),
        )
    if "pre_credit" in official_categories:
        return Decision(
            action="wait_for_credit",
            urgency="medium",
            reason="An official source says a banked reset credit will be granted soon.",
        )
    if "credit_granted" in official_categories:
        return Decision(
            action="check_local_credit",
            urgency="medium",
            reason="An official source says a banked reset credit has been granted.",
        )
    if "policy_change" in official_categories:
        return Decision(
            action="review_usage_plan",
            urgency="medium",
            reason="An official usage-limit policy change may affect quota planning.",
        )
    if "pre_reset_hint" in official_categories:
        return Decision(
            action="prefer_quota_over_credit",
            urgency="medium",
            reason="A credible milestone hint may precede a reset; keep banked credits in reserve.",
        )
    if "pre_credit" in community_categories:
        return Decision(
            action="verify_credit_signal",
            urgency="low",
            reason="Community reports mention a future credit grant; verify before waiting for it.",
        )
    if "credit_granted" in community_categories:
        return Decision(
            action="verify_credit_grant",
            urgency="low",
            reason=(
                "Community reports say a credit landed; "
                "verify the local account before relying on it."
            ),
        )
    if "policy_change" in community_categories:
        return Decision(
            action="verify_policy_change",
            urgency="low",
            reason=(
                "Community reports describe a policy change "
                "that is not yet officially verified."
            ),
        )
    if "pre_reset_hint" in community_categories:
        return Decision(
            action="verify_milestone_signal",
            urgency="low",
            reason=(
                "Community reports contain a milestone hint; "
                "monitor for an official announcement."
            ),
        )
    used = usage.primary.used_percent if usage else None
    if used is not None and used >= 95 and credits:
        return Decision(
            action="consider_credit_if_blocked",
            urgency="medium",
            reason="No reset warning is active and the primary allowance is nearly exhausted.",
        )
    return Decision(
        action="continue_normally",
        urgency="low",
        reason="No actionable reset signal or urgent credit expiry was detected.",
    )
