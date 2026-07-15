from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceItem:
    """A normalized public post, incident, topic, or issue."""

    event_id: str
    source: str
    trust: str
    title: str
    body: str = ""
    url: str = ""
    published_at: datetime | None = None

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()


@dataclass(frozen=True, slots=True)
class Alert:
    """An actionable signal derived from a public or local event."""

    event_id: str
    category: str
    stage: str
    confidence: str
    source: str
    title: str
    reason: str
    url: str = ""
    published_at: datetime | None = None
    recommended_action: str = "monitor"


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    used_percent: float | None = None
    resets_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    observed_at: datetime
    plan_type: str = ""
    primary: WindowSnapshot = field(default_factory=WindowSnapshot)
    secondary: WindowSnapshot = field(default_factory=WindowSnapshot)


@dataclass(frozen=True, slots=True)
class ResetCredit:
    status: str
    reset_type: str = "unknown"
    granted_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source: str
    ok: bool
    checked_at: datetime
    detail: str = ""
    item_count: int = 0


@dataclass(frozen=True, slots=True)
class Decision:
    action: str
    urgency: str
    reason: str


def json_ready(value: Any) -> Any:
    """Convert dataclasses and datetimes into deterministic JSON-safe values."""

    if hasattr(value, "__dataclass_fields__"):
        return json_ready(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value
