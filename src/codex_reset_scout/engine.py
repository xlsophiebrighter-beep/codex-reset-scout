from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .classify import classify_items
from .credits import query_reset_credits
from .decision import recommend
from .local_usage import compare_usage, latest_usage_snapshot
from .models import (
    Alert,
    ResetCredit,
    SourceHealth,
    UsageSnapshot,
    WindowSnapshot,
    json_ready,
)
from .notify import post_webhook
from .sources import collect_sources
from .state import StateStore

Collector = Callable[[dict[str, Any]], tuple[list[Any], list[SourceHealth]]]
Classifier = Callable[..., list[Alert]]
UsageReader = Callable[..., UsageSnapshot | None]
CreditReader = Callable[..., list[ResetCredit]]


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _usage_from_state(value: object) -> UsageSnapshot | None:
    if not isinstance(value, dict):
        return None
    observed_at = _parse_datetime(value.get("observed_at"))
    if observed_at is None:
        return None

    def window(name: str) -> WindowSnapshot:
        raw = value.get(name)
        if not isinstance(raw, dict):
            return WindowSnapshot()
        used = raw.get("used_percent")
        used_percent = float(used) if isinstance(used, (int, float)) else None
        return WindowSnapshot(
            used_percent=used_percent,
            resets_at=_parse_datetime(raw.get("resets_at")),
        )

    plan_type = value.get("plan_type")
    return UsageSnapshot(
        observed_at=observed_at,
        plan_type=plan_type if isinstance(plan_type, str) else "",
        primary=window("primary"),
        secondary=window("secondary"),
    )


def _local_alerts(
    previous: UsageSnapshot | None,
    current: UsageSnapshot | None,
    *,
    official_reset_active: bool = False,
) -> tuple[list[Alert], dict[str, dict[str, object]], dict[str, float]]:
    if previous is None or current is None:
        return [], {}, {}

    changes = compare_usage(previous, current)
    alerts: list[Alert] = []
    waste_estimates: dict[str, float] = {}
    for name in ("primary", "secondary"):
        before = getattr(previous, name)
        after = getattr(current, name)
        reset_changed = (
            before.resets_at is not None
            and after.resets_at is not None
            and before.resets_at != after.resets_at
        )
        usage_dropped = (
            before.used_percent is not None
            and after.used_percent is not None
            and after.used_percent + 5 < before.used_percent
        )
        observed_at = (
            current.observed_at
            if current.observed_at.tzinfo
            else current.observed_at.replace(tzinfo=UTC)
        )
        old_reset = before.resets_at
        if old_reset is not None and old_reset.tzinfo is None:
            old_reset = old_reset.replace(tzinfo=UTC)
        early_rebase = (
            reset_changed
            and old_reset is not None
            and old_reset.astimezone(UTC) > observed_at.astimezone(UTC) + timedelta(minutes=15)
        )
        scheduled_rollover_due = (
            old_reset is not None
            and old_reset.astimezone(UTC)
            <= observed_at.astimezone(UTC) + timedelta(minutes=15)
        )
        confirmed_arrival = (
            official_reset_active and usage_dropped and not scheduled_rollover_due
        )
        probable_arrival = early_rebase and usage_dropped
        if not confirmed_arrival and not probable_arrival and not early_rebase:
            continue

        event_time = after.resets_at or current.observed_at
        reason_parts: list[str] = []
        if early_rebase:
            reason_parts.append("the reset timestamp moved before the prior scheduled rollover")
        if usage_dropped:
            reason_parts.append(
                f"used allowance fell from {before.used_percent:.1f}% to {after.used_percent:.1f}%"
            )
        if (confirmed_arrival or probable_arrival) and before.used_percent is not None:
            remaining = round(max(0.0, 100.0 - before.used_percent), 1)
            waste_estimates[name] = remaining
            reason_parts.append(f"about {remaining:.1f}% was unused before the observed change")

        if confirmed_arrival:
            category = "post_reset_confirmation"
            confidence = "confirmed"
            title = f"Official reset signal reached the local {name} window"
        elif probable_arrival:
            category = "local_early_rebase"
            confidence = "observed"
            title = f"Local {name} window appears to have rebased early"
        else:
            category = "local_rebase_observed"
            confidence = "observed"
            title = f"Local {name} reset timestamp moved early"
        alerts.append(
            Alert(
                event_id=f"local:{name}:{event_time.isoformat()}",
                category=category,
                stage="local_arrival",
                confidence=confidence,
                source="local_codex",
                title=title,
                reason="; ".join(reason_parts),
                published_at=current.observed_at,
                recommended_action="review_local_change",
            )
        )
    return alerts, changes, waste_estimates


def _credit_summary(
    credits: list[ResetCredit], now: datetime, warning_hours: int
) -> dict[str, Any]:
    available = [credit for credit in credits if credit.status.lower() == "available"]
    deadline = now + timedelta(hours=warning_hours)
    expiring = [
        credit
        for credit in available
        if credit.expires_at is not None and credit.expires_at <= deadline
    ]
    return {
        "count": len(credits),
        "available_count": len(available),
        "expiring_count": len(expiring),
        "credits": json_ready(credits),
    }


def _credit_alerts(
    previous: object,
    credits: list[ResetCredit],
    now: datetime,
    warning_hours: int,
) -> list[Alert]:
    summary = _credit_summary(credits, now, warning_hours)
    previous_available = None
    if isinstance(previous, dict) and isinstance(previous.get("available_count"), int):
        previous_available = int(previous["available_count"])

    alerts: list[Alert] = []
    current_available = int(summary["available_count"])
    if previous_available is not None and previous_available != current_available:
        alerts.append(
            Alert(
                event_id=f"local:credit-count:{previous_available}:{current_available}",
                category="post_credit_change",
                stage="local_arrival",
                confidence="observed",
                source="local_codex",
                title="Banked reset-credit count changed",
                reason=(
                    f"available credits changed from {previous_available} "
                    f"to {current_available}"
                ),
                published_at=now,
                recommended_action="review_credit_change",
            )
        )

    deadline = now + timedelta(hours=warning_hours)
    for credit in credits:
        if (
            credit.status.lower() != "available"
            or credit.expires_at is None
            or credit.expires_at > deadline
        ):
            continue
        alerts.append(
            Alert(
                event_id=f"local:credit-expiry:{credit.expires_at.isoformat()}",
                category="credit_expiry",
                stage="local_credit",
                confidence="observed",
                source="local_codex",
                title="A banked reset credit expires soon",
                reason=(
                    f"an available reset credit expires within {warning_hours} hours "
                    f"at {credit.expires_at.isoformat()}"
                ),
                published_at=now,
                recommended_action="consider_using_expiring_credit",
            )
        )
    return alerts


def _coverage_alerts(
    health: list[SourceHealth],
    state: StateStore,
    now: datetime,
) -> list[Alert]:
    alerts: list[Alert] = []
    for item in health:
        failures = state.record_source_health(item.source, item.ok)
        if item.ok or failures < 2:
            continue
        alerts.append(
            Alert(
                event_id=f"source-health:{item.source}:{now.date().isoformat()}",
                category="source_health",
                stage="coverage_degraded",
                confidence="observed",
                source=item.source,
                title=f"Public-source coverage degraded: {item.source}",
                reason="the source could not be inspected for two consecutive checks",
                published_at=now,
                recommended_action="verify_source_manually",
            )
        )
    return alerts


def _active_alerts(alerts: list[Alert], now: datetime) -> list[Alert]:
    """Keep only signals that can still affect current quota planning."""

    confirmed_times = [
        alert.published_at
        for alert in alerts
        if alert.category == "pre_reset_confirmed" and alert.published_at is not None
    ]
    latest_confirmed = max(confirmed_times) if confirmed_times else None
    maximum_age = {
        "pre_reset_confirmed": timedelta(hours=6),
        "pre_reset_hint": timedelta(hours=36),
        "pre_credit": timedelta(hours=48),
        "credit_granted": timedelta(hours=24),
        "policy_change": timedelta(hours=48),
    }
    active: list[Alert] = []
    for alert in alerts:
        published = alert.published_at
        if (
            alert.category == "pre_reset_hint"
            and published is not None
            and latest_confirmed is not None
            and published <= latest_confirmed
        ):
            continue
        allowed_age = maximum_age.get(alert.category)
        if allowed_age is not None and published is not None:
            normalized = published if published.tzinfo else published.replace(tzinfo=UTC)
            if now - normalized.astimezone(UTC) > allowed_age:
                continue
        elif allowed_age is not None:
            continue
        active.append(alert)
    return active


def run_check(
    config: dict[str, Any],
    *,
    state_path: str | Path | None = None,
    collect: Collector = collect_sources,
    classify: Classifier = classify_items,
    usage_reader: UsageReader = latest_usage_snapshot,
    credit_reader: CreditReader = query_reset_credits,
    webhook_sender: Callable[..., None] = post_webhook,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one read-only monitoring pass and return a sanitized report."""

    now = now or datetime.now(UTC)
    state = StateStore(state_path)
    errors: list[str] = []

    try:
        items, source_health = collect(config)
    except Exception as exc:  # pragma: no cover - defensive boundary
        items = []
        source_health = [
            SourceHealth(
                source="public_sources",
                ok=False,
                checked_at=now,
                detail=f"collector failed ({type(exc).__name__})",
            )
        ]

    alerts = classify(items, min_reports=int(config.get("community_min_reports", 2)))
    alerts.extend(_coverage_alerts(source_health, state, now))
    public_active = _active_alerts(alerts, now)
    official_reset_active = any(
        alert.category == "pre_reset_confirmed"
        and alert.stage != "community_signal"
        and alert.confidence == "confirmed"
        for alert in public_active
    )

    local_config = config.get("local", {})
    local_enabled = bool(local_config.get("enabled", True))
    codex_home = local_config.get("codex_home")
    previous_local = state.get_local()
    usage: UsageSnapshot | None = None
    usage_changes: dict[str, dict[str, object]] = {}
    waste_estimates: dict[str, float] = {}
    credits: list[ResetCredit] = []

    if local_enabled:
        try:
            usage = usage_reader(codex_home=codex_home)
            local_alerts, usage_changes, waste_estimates = _local_alerts(
                _usage_from_state(previous_local.get("usage")),
                usage,
                official_reset_active=official_reset_active,
            )
            alerts.extend(local_alerts)
        except Exception as exc:  # keep public monitoring useful if local data is unavailable
            errors.append(f"local usage check failed ({type(exc).__name__})")

    include_credits = local_enabled and bool(local_config.get("include_credits", False))
    warning_hours = int(local_config.get("credit_expiry_warning_hours", 72))
    credit_summary: dict[str, Any] | None = None
    if include_credits:
        try:
            credits = credit_reader(
                enabled=True,
                codex_home=codex_home,
                timeout=int(config.get("timeout_seconds", 15)),
            )
            credit_summary = _credit_summary(credits, now, warning_hours)
            alerts.extend(
                _credit_alerts(previous_local.get("credits"), credits, now, warning_hours)
            )
        except Exception as exc:
            errors.append(f"reset-credit check failed ({type(exc).__name__})")

    active_alerts = _active_alerts(alerts, now)
    active_ids = {alert.event_id for alert in active_alerts}
    new_alerts: list[Alert] = []
    for alert in alerts:
        if state.is_seen(alert.event_id):
            continue
        if alert.event_id in active_ids:
            new_alerts.append(alert)
        else:
            state.mark_seen(alert.event_id, alert.category)

    decision = recommend(
        active_alerts,
        usage,
        credits,
        now=now,
        credit_expiry_warning_hours=warning_hours,
    )
    report: dict[str, Any] = {
        "checked_at": now.isoformat(),
        "new_alerts": json_ready(new_alerts),
        "alerts": json_ready(alerts),
        "source_health": json_ready(source_health),
        "local": {
            "enabled": local_enabled,
            "usage": json_ready(usage),
            "changes": json_ready(usage_changes),
            "estimated_unused_before_early_rebase_percent": waste_estimates,
            "credits_checked": include_credits,
            "credit_summary": credit_summary,
        },
        "decision": json_ready(decision),
        "errors": errors,
    }

    webhook_url = config.get("notifications", {}).get("webhook_url")
    delivery_ok = True
    if webhook_url and new_alerts:
        try:
            webhook_sender(
                str(webhook_url),
                {
                    "checked_at": report["checked_at"],
                    "new_alerts": report["new_alerts"],
                    "decision": report["decision"],
                },
                timeout=int(config.get("timeout_seconds", 15)),
            )
        except Exception as exc:
            delivery_ok = False
            errors.append(f"webhook delivery failed ({type(exc).__name__})")

    if delivery_ok:
        for alert in new_alerts:
            state.mark_seen(alert.event_id, alert.category)
        next_local = dict(previous_local)
        if usage is not None:
            next_local["usage"] = json_ready(usage)
        if credit_summary is not None:
            next_local["credits"] = credit_summary
        state.set_local(next_local)
    state.save()

    return report
