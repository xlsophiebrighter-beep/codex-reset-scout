from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from codex_reset_scout.sources import (
    collect_sources,
    fetch_developer_community,
    fetch_feed,
    fetch_github_issues,
    fetch_openai_status,
    fetch_reddit,
    fetch_tibo_feed,
)

NOW = datetime(2026, 7, 15, 4, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def fake_opener(routes: dict[str, bytes]):
    calls: list[str] = []

    def open_request(request: Any, timeout: int = 0) -> FakeResponse:
        del timeout
        url = request.full_url
        calls.append(url)
        if url not in routes:
            raise AssertionError(f"unexpected network request: {url}")
        return FakeResponse(routes[url])

    open_request.calls = calls
    return open_request


def as_json(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")


def test_tibo_nitter_rss_retains_canonical_x_status_link() -> None:
    url = "https://nitter.example/thsottiaux/rss"
    rss = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><item>
      <guid>https://nitter.example/thsottiaux/status/2077114635308986427</guid>
      <link>https://nitter.example/thsottiaux/status/2077114635308986427#m</link>
      <title>Codex usage limits will be reset in the next hour</title>
      <description><![CDATA[<p>Another full reset is coming.</p>]]></description>
      <pubDate>Wed, 15 Jul 2026 03:34:54 GMT</pubDate>
    </item></channel></rss>"""

    items, health = fetch_tibo_feed(url, opener=fake_opener({url: rss}), now=NOW)

    assert health.ok is True
    assert health.item_count == 1
    assert items[0].event_id == "tibo:2077114635308986427"
    assert items[0].url == "https://x.com/thsottiaux/status/2077114635308986427"
    assert items[0].trust == "tibo"
    assert items[0].published_at == datetime(2026, 7, 15, 3, 34, 54, tzinfo=UTC)


def test_openai_status_json_uses_latest_update_in_event_id() -> None:
    url = "https://status.example/incidents.json"
    payload = {
        "incidents": [
            {
                "id": "inc-1",
                "name": "Codex usage update",
                "status": "monitoring",
                "impact": "none",
                "shortlink": "https://status.openai.com/incidents/inc-1",
                "incident_updates": [
                    {
                        "id": "update-2",
                        "body": "Codex usage limits will reset later today.",
                        "created_at": "2026-07-15T03:00:00Z",
                    }
                ],
            }
        ]
    }

    items, health = fetch_openai_status(
        url=url, opener=fake_opener({url: as_json(payload)}), now=NOW
    )

    assert health.ok is True
    assert items[0].event_id == "openai-status:inc-1:update-2"
    assert items[0].trust == "official"
    assert "will reset later today" in items[0].body


def test_developer_community_latest_json_is_normalized() -> None:
    url = "https://community.example/latest.json"
    payload = {
        "topic_list": {
            "topics": [
                {
                    "id": 42,
                    "slug": "codex-reset-report",
                    "title": "Codex reset expected later",
                    "created_at": "2026-07-15T02:00:00Z",
                }
            ]
        }
    }

    items, health = fetch_developer_community(
        url=url, opener=fake_opener({url: as_json(payload)}), now=NOW
    )

    assert health.ok is True
    assert items[0].event_id == "developer-community:42"
    assert items[0].trust == "community"
    assert items[0].url == "https://community.openai.com/t/codex-reset-report/42"


def test_github_issues_filters_pull_requests_and_marks_maintainers() -> None:
    url = "https://api.github.example/issues"
    payload = [
        {
            "id": 11,
            "number": 1,
            "title": "Usage reset scheduled tomorrow",
            "body": "A maintainer update",
            "html_url": "https://github.com/openai/codex/issues/1",
            "created_at": "2026-07-15T01:00:00Z",
            "author_association": "MEMBER",
        },
        {
            "id": 12,
            "title": "A pull request",
            "pull_request": {"url": "https://api.github.example/pulls/2"},
        },
    ]

    items, health = fetch_github_issues(
        url=url, opener=fake_opener({url: as_json(payload)}), now=NOW
    )

    assert health.ok is True
    assert health.item_count == 1
    assert items[0].event_id == "github-issue:11"
    assert items[0].trust == "maintainer"


def test_reddit_new_json_is_normalized() -> None:
    url = "https://reddit.example/new.json"
    payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "id": "abc123",
                        "title": "Reset banner appeared",
                        "selftext": "It says the reset will arrive in one hour.",
                        "permalink": "/r/codex/comments/abc123/reset_banner/",
                        "created_utc": 1784084400,
                    }
                }
            ]
        }
    }

    items, health = fetch_reddit(url=url, opener=fake_opener({url: as_json(payload)}), now=NOW)

    assert health.ok is True
    assert items[0].event_id == "reddit:abc123"
    assert items[0].url.startswith("https://www.reddit.com/r/codex/")
    assert items[0].trust == "community"


def test_reddit_atom_feed_is_supported() -> None:
    url = "https://reddit.example/new.rss"
    atom = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>t3_abc123</id>
        <title>Reset banner appeared</title>
        <content>It says the reset will arrive in one hour.</content>
        <link href="https://www.reddit.com/r/codex/comments/abc123/reset_banner/" />
        <updated>2026-07-15T03:30:00Z</updated>
      </entry>
    </feed>"""

    items, health = fetch_reddit(url=url, opener=fake_opener({url: atom}), now=NOW)

    assert health.ok is True
    assert items[0].event_id == "reddit:abc123"
    assert items[0].title == "Reset banner appeared"
    assert items[0].trust == "community"


def test_generic_atom_feed_is_supported() -> None:
    url = "https://feed.example/updates.atom"
    atom = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>tag:example,2026:one</id>
        <title>Codex policy update</title>
        <summary>The weekly limit has increased.</summary>
        <link rel="alternate" href="https://feed.example/posts/one" />
        <updated>2026-07-15T03:30:00Z</updated>
      </entry>
    </feed>"""

    items, health = fetch_feed(
        url,
        source="news_feed",
        opener=fake_opener({url: atom}),
        now=NOW,
    )

    assert health.ok is True
    assert items[0].source == "news_feed"
    assert items[0].url == "https://feed.example/posts/one"
    assert items[0].body == "The weekly limit has increased."


def test_collect_sources_honors_config_lookback_and_extra_feeds() -> None:
    url = "https://feed.example/news.rss"
    rss = b"""<rss version="2.0"><channel>
      <item><guid>new</guid><title>New item</title>
        <pubDate>Wed, 15 Jul 2026 03:00:00 GMT</pubDate></item>
      <item><guid>old</guid><title>Old item</title>
        <pubDate>Sun, 12 Jul 2026 03:00:00 GMT</pubDate></item>
    </channel></rss>"""
    config = {
        "lookback_hours": 48,
        "timeout_seconds": 3,
        "sources": {
            "tibo_feed_urls": [],
            "openai_status": False,
            "developer_community": False,
            "github_issues": False,
            "reddit": False,
            "extra_feeds": [{"url": url, "name": "custom", "trust": "official"}],
        },
    }
    opener = fake_opener({url: rss})

    items, health = collect_sources(config, opener=opener, now=NOW)

    assert [entry.title for entry in items] == ["New item"]
    assert len(health) == 1
    assert health[0].source == "custom"
    assert opener.calls == [url]


def test_collect_sources_ignores_undated_feed_entries() -> None:
    url = "https://feed.example/undated.rss"
    rss = b"""<rss version="2.0"><channel>
      <item><guid>undated</guid><title>Reset later today</title></item>
    </channel></rss>"""
    config = {
        "lookback_hours": 48,
        "timeout_seconds": 3,
        "sources": {
            "tibo_feed_urls": [],
            "openai_status": False,
            "developer_community": False,
            "github_issues": False,
            "reddit": False,
            "extra_feeds": [{"url": url, "name": "custom", "trust": "official"}],
        },
    }

    items, health = collect_sources(config, opener=fake_opener({url: rss}), now=NOW)

    assert items == []
    assert health[0].ok is True


def test_network_failure_returns_unhealthy_status_instead_of_raising() -> None:
    def failing_opener(request: Any, timeout: int = 0) -> FakeResponse:
        del request, timeout
        raise OSError("network unavailable")

    items, health = fetch_reddit(opener=failing_opener, now=NOW)

    assert items == []
    assert health.ok is False
    assert health.item_count == 0
    assert "network unavailable" in health.detail
