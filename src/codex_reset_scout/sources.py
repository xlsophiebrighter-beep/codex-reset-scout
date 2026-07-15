from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from .models import SourceHealth, SourceItem

OPENAI_STATUS_URL = "https://status.openai.com/api/v2/incidents.json"
DEVELOPER_COMMUNITY_URL = "https://community.openai.com/latest.json"
GITHUB_ISSUES_URL = (
    "https://api.github.com/repos/openai/codex/issues"
    "?state=all&sort=created&direction=desc&per_page=50"
)
REDDIT_NEW_URL = "https://www.reddit.com/r/codex/new/.rss"

Opener = Callable[..., Any]


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"br", "p", "div", "li"}:
            self.parts.append(" ")


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    parser = _HTMLText()
    try:
        parser.feed(html.unescape(str(value)))
        text = " ".join(parser.parts)
    except Exception:
        text = str(value)
    return " ".join(text.split())


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _stable_id(*parts: str) -> str:
    payload = "\n".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()[:24]


def _error_detail(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, TimeoutError):
        return "request timed out"
    detail = " ".join(str(exc).split()).casefold()
    if "network unavailable" in detail:
        return "network unavailable"
    if isinstance(exc, (json.JSONDecodeError, ET.ParseError)):
        return "unsupported response format"
    return type(exc).__name__


def _read_url(
    url: str,
    *,
    timeout: int,
    opener: Opener | None,
    accept: str,
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": (
                "codex-reset-scout/0.1 "
                "(+https://github.com/xlsophiebrighter-beep/codex-reset-scout)"
            ),
        },
    )
    open_request = opener or urllib.request.urlopen
    response = open_request(request, timeout=timeout)
    if hasattr(response, "__enter__"):
        with response as stream:
            status = getattr(stream, "status", None)
            if status is not None and int(status) >= 400:
                raise urllib.error.HTTPError(url, int(status), "HTTP error", {}, None)
            return stream.read()
    try:
        status = getattr(response, "status", None)
        if status is not None and int(status) >= 400:
            raise urllib.error.HTTPError(url, int(status), "HTTP error", {}, None)
        return response.read()
    finally:
        close = getattr(response, "close", None)
        if close:
            close()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _child_text(element: ET.Element, names: Iterable[str]) -> str:
    wanted = {name.casefold() for name in names}
    for child in list(element):
        if _local_name(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


def _entry_link(element: ET.Element) -> str:
    for child in list(element):
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        rel = child.attrib.get("rel", "alternate")
        if href and rel in {"", "alternate"}:
            return href.strip()
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def _parse_feed(payload: bytes, source: str, trust: str) -> list[SourceItem]:
    root = ET.fromstring(payload)
    entries = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    items: list[SourceItem] = []
    for entry in entries:
        title = _clean_text(_child_text(entry, ("title",)))
        body = _clean_text(_child_text(entry, ("description", "summary", "content", "encoded")))
        url = _entry_link(entry)
        guid = _child_text(entry, ("guid", "id"))
        published = _parse_datetime(_child_text(entry, ("pubdate", "published", "updated", "date")))
        if not (title or body):
            continue
        stable = guid or url or _stable_id(title, body, str(published or ""))
        items.append(
            SourceItem(
                event_id=f"{source}:{_stable_id(stable)}",
                source=source,
                trust=trust,
                title=title or body[:160],
                body=body,
                url=url,
                published_at=published,
            )
        )
    return items


def fetch_feed(
    url: str,
    *,
    source: str = "extra_feed",
    trust: str = "community",
    timeout: int = 15,
    opener: Opener | None = None,
    now: datetime | None = None,
) -> tuple[list[SourceItem], SourceHealth]:
    """Fetch an RSS or Atom feed and normalize its entries."""

    checked_at = _now(now)
    try:
        payload = _read_url(
            url,
            timeout=timeout,
            opener=opener,
            accept="application/atom+xml, application/rss+xml, application/xml, text/xml",
        )
        items = _parse_feed(payload, source, trust)
        return items, SourceHealth(source, True, checked_at, item_count=len(items))
    except Exception as exc:
        return [], SourceHealth(source, False, checked_at, _error_detail(exc), 0)


_STATUS_ID = re.compile(r"/(?:status|statuses)/(\d+)", re.IGNORECASE)


def fetch_tibo_feed(
    url: str,
    *,
    timeout: int = 15,
    opener: Opener | None = None,
    now: datetime | None = None,
) -> tuple[list[SourceItem], SourceHealth]:
    """Fetch Tibo's X/Nitter RSS and retain canonical x.com status links."""

    items, health = fetch_feed(
        url,
        source="tibo",
        trust="tibo",
        timeout=timeout,
        opener=opener,
        now=now,
    )
    host = (urllib.parse.urlsplit(url).hostname or "unknown").casefold()
    health = SourceHealth(
        source=f"tibo:{host}",
        ok=health.ok,
        checked_at=health.checked_at,
        detail=health.detail,
        item_count=health.item_count,
    )
    normalized: list[SourceItem] = []
    for item in items:
        match = _STATUS_ID.search(f"{item.url}\n{item.text}")
        if match:
            status_id = match.group(1)
            event_id = f"tibo:{status_id}"
            item_url = f"https://x.com/thsottiaux/status/{status_id}"
        else:
            event_id = item.event_id
            item_url = item.url
        normalized.append(
            SourceItem(
                event_id=event_id,
                source="tibo",
                trust="tibo",
                title=item.title,
                body=item.body,
                url=item_url,
                published_at=item.published_at,
            )
        )
    return normalized, health


def _json_payload(
    url: str,
    *,
    timeout: int,
    opener: Opener | None,
) -> Any:
    payload = _read_url(
        url,
        timeout=timeout,
        opener=opener,
        accept="application/json",
    )
    return json.loads(payload.decode("utf-8-sig"))


def fetch_openai_status(
    *,
    timeout: int = 15,
    opener: Opener | None = None,
    now: datetime | None = None,
    url: str = OPENAI_STATUS_URL,
) -> tuple[list[SourceItem], SourceHealth]:
    checked_at = _now(now)
    source = "openai_status"
    try:
        payload = _json_payload(url, timeout=timeout, opener=opener)
        incidents = []
        if isinstance(payload, dict):
            incidents.extend(payload.get("incidents") or [])
            incidents.extend(payload.get("scheduled_maintenances") or [])
        items: list[SourceItem] = []
        for incident in incidents:
            if not isinstance(incident, dict):
                continue
            incident_id = str(incident.get("id") or _stable_id(str(incident)))
            updates = incident.get("incident_updates") or []
            latest = updates[0] if updates and isinstance(updates[0], dict) else {}
            update_id = str(latest.get("id") or "initial")
            body_parts = [
                str(incident.get("status") or ""),
                str(incident.get("impact") or ""),
                str(latest.get("body") or ""),
            ]
            items.append(
                SourceItem(
                    event_id=f"openai-status:{incident_id}:{update_id}",
                    source=source,
                    trust="official",
                    title=_clean_text(incident.get("name") or "OpenAI status update"),
                    body=_clean_text(" ".join(body_parts)),
                    url=str(
                        incident.get("shortlink")
                        or f"https://status.openai.com/incidents/{incident_id}"
                    ),
                    published_at=_parse_datetime(
                        latest.get("created_at")
                        or incident.get("updated_at")
                        or incident.get("created_at")
                    ),
                )
            )
        return items, SourceHealth(source, True, checked_at, item_count=len(items))
    except Exception as exc:
        return [], SourceHealth(source, False, checked_at, _error_detail(exc), 0)


def fetch_developer_community(
    *,
    timeout: int = 15,
    opener: Opener | None = None,
    now: datetime | None = None,
    url: str = DEVELOPER_COMMUNITY_URL,
) -> tuple[list[SourceItem], SourceHealth]:
    checked_at = _now(now)
    source = "developer_community"
    try:
        payload = _json_payload(url, timeout=timeout, opener=opener)
        topics = (
            payload.get("topic_list", {}).get("topics", []) if isinstance(payload, dict) else []
        )
        items: list[SourceItem] = []
        for topic in topics:
            if not isinstance(topic, dict) or topic.get("id") is None:
                continue
            topic_id = str(topic["id"])
            slug = str(topic.get("slug") or "topic")
            title = _clean_text(topic.get("title") or topic.get("fancy_title") or "")
            items.append(
                SourceItem(
                    event_id=f"developer-community:{topic_id}",
                    source=source,
                    trust="community",
                    title=title,
                    body=_clean_text(topic.get("excerpt") or ""),
                    url=f"https://community.openai.com/t/{slug}/{topic_id}",
                    published_at=_parse_datetime(
                        topic.get("created_at") or topic.get("last_posted_at")
                    ),
                )
            )
        return items, SourceHealth(source, True, checked_at, item_count=len(items))
    except Exception as exc:
        return [], SourceHealth(source, False, checked_at, _error_detail(exc), 0)


def fetch_github_issues(
    *,
    timeout: int = 15,
    opener: Opener | None = None,
    now: datetime | None = None,
    url: str = GITHUB_ISSUES_URL,
) -> tuple[list[SourceItem], SourceHealth]:
    checked_at = _now(now)
    source = "github_issues"
    try:
        payload = _json_payload(url, timeout=timeout, opener=opener)
        issues = payload if isinstance(payload, list) else []
        items: list[SourceItem] = []
        for issue in issues:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            issue_id = str(issue.get("id") or issue.get("number") or _stable_id(str(issue)))
            association = str(issue.get("author_association") or "").upper()
            trust = (
                "maintainer" if association in {"OWNER", "MEMBER", "COLLABORATOR"} else "community"
            )
            items.append(
                SourceItem(
                    event_id=f"github-issue:{issue_id}",
                    source=source,
                    trust=trust,
                    title=_clean_text(issue.get("title") or ""),
                    body=_clean_text(issue.get("body") or "")[:8000],
                    url=str(issue.get("html_url") or ""),
                    published_at=_parse_datetime(
                        issue.get("created_at") or issue.get("updated_at")
                    ),
                )
            )
        return items, SourceHealth(source, True, checked_at, item_count=len(items))
    except Exception as exc:
        return [], SourceHealth(source, False, checked_at, _error_detail(exc), 0)


def fetch_reddit(
    *,
    timeout: int = 15,
    opener: Opener | None = None,
    now: datetime | None = None,
    url: str = REDDIT_NEW_URL,
) -> tuple[list[SourceItem], SourceHealth]:
    checked_at = _now(now)
    source = "reddit"
    try:
        raw = _read_url(
            url,
            timeout=timeout,
            opener=opener,
            accept="application/atom+xml, application/rss+xml, application/json",
        )
        if raw.lstrip().startswith((b"{", b"[")):
            payload = json.loads(raw.decode("utf-8-sig"))
        else:
            feed_items = _parse_feed(raw, source, "community")
            normalized: list[SourceItem] = []
            for item in feed_items:
                match = re.search(r"/comments/([^/?#]+)", item.url)
                event_id = f"reddit:{match.group(1)}" if match else item.event_id
                normalized.append(
                    SourceItem(
                        event_id=event_id,
                        source=source,
                        trust="community",
                        title=item.title,
                        body=item.body,
                        url=item.url,
                        published_at=item.published_at,
                    )
                )
            return normalized, SourceHealth(
                source, True, checked_at, item_count=len(normalized)
            )
        children = payload.get("data", {}).get("children", []) if isinstance(payload, dict) else []
        items: list[SourceItem] = []
        for child in children:
            data = child.get("data", {}) if isinstance(child, dict) else {}
            if not isinstance(data, dict) or not data.get("id"):
                continue
            permalink = str(data.get("permalink") or "")
            url_value = (
                urllib.parse.urljoin("https://www.reddit.com", permalink)
                if permalink
                else str(data.get("url") or "")
            )
            items.append(
                SourceItem(
                    event_id=f"reddit:{data['id']}",
                    source=source,
                    trust="community",
                    title=_clean_text(data.get("title") or ""),
                    body=_clean_text(data.get("selftext") or "")[:8000],
                    url=url_value,
                    published_at=_parse_datetime(data.get("created_utc")),
                )
            )
        return items, SourceHealth(source, True, checked_at, item_count=len(items))
    except Exception as exc:
        return [], SourceHealth(source, False, checked_at, _error_detail(exc), 0)


def _within_lookback(item: SourceItem, cutoff: datetime) -> bool:
    return item.published_at is not None and item.published_at >= cutoff


def collect_sources(
    config: dict[str, Any],
    *,
    opener: Opener | None = None,
    now: datetime | None = None,
) -> tuple[list[SourceItem], list[SourceHealth]]:
    """Collect enabled public sources using the existing config schema."""

    checked_at = _now(now)
    timeout = int(config.get("timeout_seconds", 15))
    source_config = config.get("sources", {})
    items: list[SourceItem] = []
    health: list[SourceHealth] = []

    def collect(result: tuple[list[SourceItem], SourceHealth]) -> None:
        found, status = result
        items.extend(found)
        health.append(status)

    for feed_url in source_config.get("tibo_feed_urls", []) or []:
        collect(fetch_tibo_feed(str(feed_url), timeout=timeout, opener=opener, now=checked_at))
    if source_config.get("openai_status", False):
        collect(fetch_openai_status(timeout=timeout, opener=opener, now=checked_at))
    if source_config.get("developer_community", False):
        collect(fetch_developer_community(timeout=timeout, opener=opener, now=checked_at))
    if source_config.get("github_issues", False):
        collect(fetch_github_issues(timeout=timeout, opener=opener, now=checked_at))
    if source_config.get("reddit", False):
        collect(fetch_reddit(timeout=timeout, opener=opener, now=checked_at))

    for index, feed in enumerate(source_config.get("extra_feeds", []) or []):
        if isinstance(feed, dict):
            feed_url = str(feed.get("url") or "")
            source = str(feed.get("name") or f"extra_feed_{index + 1}")
            trust = str(feed.get("trust") or "community")
        else:
            feed_url = str(feed)
            source = f"extra_feed_{index + 1}"
            trust = "community"
        if feed_url:
            collect(
                fetch_feed(
                    feed_url,
                    source=source,
                    trust=trust,
                    timeout=timeout,
                    opener=opener,
                    now=checked_at,
                )
            )

    lookback = max(0, int(config.get("lookback_hours", 48)))
    cutoff = checked_at - timedelta(hours=lookback)
    deduplicated: dict[str, SourceItem] = {}
    for item in items:
        if _within_lookback(item, cutoff):
            deduplicated[item.event_id] = item
    return list(deduplicated.values()), health


# Readable aliases for callers and integrations.
collect_public_sources = collect_sources
fetch_tibo_rss = fetch_tibo_feed
fetch_extra_feed = fetch_feed
