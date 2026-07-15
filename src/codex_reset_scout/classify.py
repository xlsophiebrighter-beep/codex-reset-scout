from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import Alert, SourceItem

_RESET_TERM = re.compile(
    r"\b(?:codex\s+)?(?:usage[- ]limit\s+|rate[- ]limit\s+|full\s+|hard\s+)?reset\b"
    r"|\breset(?:ting)?\s+(?:all\s+)?(?:the\s+)?(?:codex\s+)?(?:usage\s+)?limits?\b",
    re.IGNORECASE,
)
_EXPLICIT_RESET = (
    re.compile(
        r"\b(?:will|we(?:'|’)ll|shall|should|going\s+to|about\s+to|expected\s+to|"
        r"scheduled\s+to|plan(?:ned)?\s+to)\b.{0,100}\breset(?:ting)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\breset\b.{0,100}\b(?:today|tomorrow|later|soon|incoming|imminent|"
        r"next\s+(?:\d+\s+)?(?:minutes?|hours?|days?)|over\s+the\s+next|"
        r"in\s+the\s+next|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:incoming|imminent|planned|scheduled)\b.{0,60}\breset\b"
        r"|\breset\b.{0,60}\b(?:should\s+land|will\s+land|rolling\s+out)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:we\s+)?(?:are|(?:'|’)re)\s+(?:once\s+again\s+)?resetting\b",
        re.IGNORECASE,
    ),
)
_NO_RESET = (
    re.compile(
        r"\b(?:no|not|won(?:'|’)t|will\s+not|isn(?:'|’)t|is\s+not|aren(?:'|’)t|"
        r"are\s+not)\s+(?:(?:another|further|codex)\s+)*"
        r"(?:full\s+|hard\s+|usage[- ]limit\s+)?resets?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+plans?\s+to\b.{0,50}\breset\b"
        r"|\b(?:we\s+are|we(?:'|’)re|i\s+am|i(?:'|’)m)(?:\s+currently)?\s+not\s+"
        r"(?:planning|going)\s+to\b.{0,50}\breset(?:ting)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\breset\b.{0,40}\b(?:is\s+)?not\s+(?:planned|coming|happening|scheduled)\b"
        r"|\bthis\s+is\s+not\s+(?:a\s+)?reset\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\breset\b.{0,50}(?:[.!?]\s*)?but\s+no\b"
        r"|\bnot\s+(?:actually\s+)?(?:announc(?:e|ing)|doing|triggering|applying)"
        r".{0,40}\breset\b",
        re.IGNORECASE,
    ),
)
_CREDIT_TERM = re.compile(
    r"\b(?:banked\s+)?reset\s+(?:cards?|credits?)\b"
    r"|\brate[- ]limit\s+reset\s+credits?\b"
    r"|\bbanked\s+reset\b",
    re.IGNORECASE,
)
_CREDIT_GRANTED = re.compile(
    r"\b(?:have|has|we(?:'|’)ve)\s+(?:added|granted|gifted)\b"
    r"|\b(?:is|are)\s+now\s+available\b"
    r"|\byou\s+can\s+(?:apply|use|redeem)\s+(?:the|your|a)\s+reset\b",
    re.IGNORECASE,
)
_PRE_CREDIT = re.compile(
    r"\b(?:will|we(?:'|’)ll|going\s+to|plan(?:ned)?\s+to|tomorrow|later|soon|"
    r"coming|grant(?:ing)?|gift(?:ing)?|add(?:ing)?|ship(?:ping)?|rolling\s+out|"
    r"made\s+available|receive|get)\b",
    re.IGNORECASE,
)
_POLICY_TERM = re.compile(
    r"\b(?:5[- ]?hour|5h|weekly|seven[- ]day|7[- ]day)\s+(?:usage\s+)?limits?\b"
    r"|\b(?:usage|rate[- ]limit)\s+(?:window|period|policy)\b"
    r"|\b(?:models?|gpt[- ]?[\w.]+)\b.{0,30}\b(?:against|toward)\s+(?:the\s+)?limits?\b",
    re.IGNORECASE,
)
_POLICY_CHANGE = re.compile(
    r"\b(?:remove(?:d|s|ing)?|suspend(?:ed|s|ing)?|pause(?:d|s|ing)?|"
    r"change(?:d|s|ing)?|increase(?:d|s|ing)?|decrease(?:d|s|ing)?|"
    r"extend(?:ed|s|ing)?|shorten(?:ed|s|ing)?|cheaper|costs?\s+less|"
    r"rebas(?:e|ed|es|ing)|no\s+longer|temporar(?:y|ily)|double(?:d|s|ing)?)\b",
    re.IGNORECASE,
)
_NO_POLICY_CHANGE = re.compile(
    r"\b(?:no|without)\s+(?:planned\s+)?changes?\b"
    r"|\b(?:will\s+not|won(?:'|’)t|is\s+not)\b.{0,40}\b"
    r"(?:change|remove|suspend|increase|decrease)\b",
    re.IGNORECASE,
)
_MILESTONE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:m|million)\s+(?:daily\s+)?active\s+users?\b"
    r"|\b(?:active[- ]user|growth)\s+milestone\b"
    r"|\bmilestone\s+(?:celebration|reward|day)\b",
    re.IGNORECASE,
)
_MILESTONE_HINT = re.compile(
    r"\b(?:tomorrow|later|soon|celebrat(?:e|es|ed|ing|ion)|reward|just\s+saying|"
    r"see\s+you\s+tomorrow|might\s+be|could\s+be|stay\s+tuned|more\s+updates?)\b",
    re.IGNORECASE,
)
_MILESTONE_NUMBER = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:m|million)\b", re.IGNORECASE)
_COMMUNITY_WINDOW = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class _Category:
    category: str
    stage: str
    confidence: str
    reason: str
    recommended_action: str


_CATEGORIES = {
    "pre_reset_confirmed": _Category(
        category="pre_reset_confirmed",
        stage="pre_warning",
        confidence="confirmed",
        reason="The source explicitly says a Codex usage reset is planned or imminent.",
        recommended_action="spend_remaining_quota",
    ),
    "pre_reset_hint": _Category(
        category="pre_reset_hint",
        stage="hint",
        confidence="probable",
        reason="A milestone or celebration hint may precede a Codex reset or reward.",
        recommended_action="prefer_quota_over_credit",
    ),
    "pre_credit": _Category(
        category="pre_credit",
        stage="pre_warning",
        confidence="confirmed",
        reason="The source announces a future banked reset card or credit grant.",
        recommended_action="wait_for_credit",
    ),
    "credit_granted": _Category(
        category="credit_granted",
        stage="announcement",
        confidence="confirmed",
        reason="The source says a banked reset has already been granted or made available.",
        recommended_action="check_local_credit",
    ),
    "policy_change": _Category(
        category="policy_change",
        stage="policy_change",
        confidence="confirmed",
        reason="The source announces a material Codex usage-limit policy change.",
        recommended_action="review_usage_plan",
    ),
}

_COMMUNITY_ACTIONS = {
    "pre_reset_confirmed": "verify_and_prepare_quota",
    "pre_reset_hint": "verify_milestone_signal",
    "pre_credit": "verify_credit_signal",
    "credit_granted": "verify_credit_grant",
    "policy_change": "verify_policy_change",
}


def _normalized(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").split())


def _matches_any(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _milestone_key(text: str) -> str | None:
    values: set[str] = set()
    for match in _MILESTONE_NUMBER.finditer(text):
        value = match.group(1).lstrip("0") or "0"
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        values.add(f"{value}m")
    return "+".join(sorted(values)) if values else None


def _published_utc(item: SourceItem) -> datetime | None:
    published = item.published_at
    if published is None:
        return None
    if published.tzinfo is None:
        return published.replace(tzinfo=UTC)
    return published.astimezone(UTC)


def _category_for(text: str) -> str | None:
    text = _normalized(text)
    reset_is_negated = _matches_any(_NO_RESET, text)

    if _CREDIT_TERM.search(text) and _CREDIT_GRANTED.search(text):
        return "credit_granted"
    if _CREDIT_TERM.search(text) and _PRE_CREDIT.search(text):
        return "pre_credit"
    if not reset_is_negated and _RESET_TERM.search(text) and _matches_any(_EXPLICIT_RESET, text):
        return "pre_reset_confirmed"
    if (
        _POLICY_TERM.search(text)
        and _POLICY_CHANGE.search(text)
        and not _NO_POLICY_CHANGE.search(text)
    ):
        return "policy_change"
    if not reset_is_negated and _MILESTONE.search(text) and _MILESTONE_HINT.search(text):
        return "pre_reset_hint"
    return None


def _is_direct_source(item: SourceItem) -> bool:
    return item.trust.casefold() in {"official", "tibo"}


def _alert_for(item: SourceItem, category: str) -> Alert:
    metadata = _CATEGORIES[category]
    return Alert(
        event_id=item.event_id,
        category=metadata.category,
        stage=metadata.stage,
        confidence=metadata.confidence,
        source=item.source,
        title=item.title,
        reason=metadata.reason,
        url=item.url,
        published_at=item.published_at,
        recommended_action=metadata.recommended_action,
    )


def classify_item(item: SourceItem) -> Alert | None:
    """Classify one direct official/Tibo item.

    Community items intentionally return ``None`` here. Use :func:`classify_items`
    to enforce corroboration before a community signal becomes actionable.
    """

    category = _category_for(item.text)
    if category is None or not _is_direct_source(item):
        return None
    return _alert_for(item, category)


def _community_alert(category: str, items: list[SourceItem]) -> Alert:
    metadata = _CATEGORIES[category]
    ordered = sorted(items, key=lambda item: item.event_id)
    digest = hashlib.sha256("\n".join(item.event_id for item in ordered).encode()).hexdigest()[:16]
    published = [item.published_at for item in ordered if item.published_at is not None]
    sources = ", ".join(sorted({item.source for item in ordered}))
    return Alert(
        event_id=f"community:{category}:{digest}",
        category=metadata.category,
        stage="community_signal",
        confidence="medium",
        source="community",
        title=f"{len(ordered)} corroborating community reports: {ordered[0].title}",
        reason=(
            f"{len(ordered)} independent sources ({sources}) match {category} within six hours; "
            "this is community corroboration, not an official announcement."
        ),
        url=ordered[0].url,
        published_at=max(published) if published else None,
        recommended_action=_COMMUNITY_ACTIONS[category],
    )


def _community_alerts(
    category: str,
    items: Iterable[SourceItem],
    threshold: int,
) -> list[Alert]:
    timed = sorted(
        (
            (published, item)
            for item in items
            if (published := _published_utc(item)) is not None
        ),
        key=lambda entry: (entry[0], entry[1].event_id),
    )
    alerts: list[Alert] = []
    start = 0
    while start < len(timed):
        anchor = timed[start][0]
        end = start
        while end < len(timed) and timed[end][0] - anchor <= _COMMUNITY_WINDOW:
            end += 1

        by_source: dict[str, SourceItem] = {}
        for _published, item in timed[start:end]:
            by_source.setdefault(item.source.casefold(), item)
        if len(by_source) >= threshold:
            alerts.append(_community_alert(category, list(by_source.values())))
            start = end
        else:
            start += 1
    return alerts


def classify_items(items: Iterable[SourceItem], min_reports: int = 2) -> list[Alert]:
    """Return only actionable alerts, requiring corroboration for community reports."""

    threshold = max(1, int(min_reports))
    alerts: list[Alert] = []
    community: dict[tuple[str, str | None], dict[str, SourceItem]] = defaultdict(dict)

    for item in items:
        category = _category_for(item.text)
        if category is None:
            continue
        if _is_direct_source(item):
            alerts.append(_alert_for(item, category))
        elif item.published_at is not None:
            key = (category, _milestone_key(item.text))
            community[key][item.event_id] = item

    for category, milestone in sorted(community, key=lambda key: (key[0], key[1] or "")):
        reports = community[(category, milestone)].values()
        alerts.extend(_community_alerts(category, reports, threshold))

    return alerts
