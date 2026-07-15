from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime

import pytest

from codex_reset_scout.credits import (
    RESET_CREDITS_ENDPOINT,
    CreditQueryError,
    configured_reset_credits,
    query_reset_credits,
)


class FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self.body = json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class FakeOpener:
    def __init__(self, payload=None, error=None) -> None:
        self.payload = payload
        self.error = error
        self.calls = []

    def open(self, request, timeout: int):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


def _write_auth(codex_home, access_token: str, account_id: str = "account-secret") -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": access_token, "account_id": account_id}}),
        encoding="utf-8",
    )


def test_query_requires_explicit_opt_in_before_auth_or_network_access(tmp_path) -> None:
    opener = FakeOpener([])

    with pytest.raises(PermissionError, match="explicit opt-in"):
        query_reset_credits(enabled=False, codex_home=tmp_path / "missing", opener=opener)

    assert opener.calls == []


def test_query_is_read_only_and_returns_only_sanitized_credit_fields(tmp_path) -> None:
    token = "header.payload.signature-super-secret"
    account_id = "account-secret"
    _write_auth(tmp_path, token, account_id)
    opener = FakeOpener(
        {
            "credits": [
                {
                    "id": "credit-secret-id",
                    "status": "available",
                    "reset_type": "weekly",
                    "granted_at": "2026-07-01T00:00:00Z",
                    "expires_at": "2026-07-20T00:00:00Z",
                    "unexpected": {"token": "response-secret"},
                }
            ],
            "account_id": account_id,
        }
    )

    credits = query_reset_credits(enabled=True, codex_home=tmp_path, opener=opener)

    assert len(credits) == 1
    assert credits[0].status == "available"
    assert credits[0].reset_type == "weekly"
    assert credits[0].granted_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert credits[0].expires_at == datetime(2026, 7, 20, tzinfo=UTC)
    rendered = repr(credits)
    for secret in (token, account_id, "credit-secret-id", "response-secret"):
        assert secret not in rendered

    request, timeout = opener.calls[0]
    assert request.full_url == RESET_CREDITS_ENDPOINT
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {token}"
    assert request.get_header("Chatgpt-account-id") == account_id
    assert timeout == 15


def test_query_tolerates_camel_case_nested_data_and_unknown_items(tmp_path) -> None:
    _write_auth(tmp_path, "opaque-token")
    opener = FakeOpener(
        {
            "data": {
                "resetCredits": [
                    {"renamed_future_field": "ignored"},
                    {
                        "state": "available",
                        "resetType": "full",
                        "grantedAt": 1782864000000,
                        "expiresAt": "2026-08-01T00:00:00+00:00",
                    },
                ]
            }
        }
    )

    credits = query_reset_credits(enabled=True, codex_home=tmp_path, opener=opener)

    assert len(credits) == 1
    assert credits[0].status == "available"
    assert credits[0].reset_type == "full"
    assert credits[0].granted_at == datetime(2026, 7, 1, tzinfo=UTC)


def test_config_flag_is_the_opt_in_and_disabled_config_does_not_read_auth(tmp_path) -> None:
    opener = FakeOpener([])
    disabled = {
        "timeout_seconds": 3,
        "local": {"include_credits": False, "codex_home": str(tmp_path / "missing")},
    }
    assert configured_reset_credits(disabled, opener=opener) == []
    assert opener.calls == []

    _write_auth(tmp_path, "opaque-token")
    enabled = {
        "timeout_seconds": 3,
        "local": {"include_credits": True, "codex_home": str(tmp_path)},
    }
    assert configured_reset_credits(enabled, opener=opener) == []
    assert opener.calls[0][1] == 3


def test_network_error_is_sanitized(tmp_path) -> None:
    token = "must-not-appear"
    _write_auth(tmp_path, token)
    opener = FakeOpener(error=urllib.error.URLError(f"failed while using {token}"))

    with pytest.raises(CreditQueryError) as captured:
        query_reset_credits(enabled=True, codex_home=tmp_path, opener=opener)

    assert str(captured.value) == "Reset-credit query failed"
    assert token not in str(captured.value)
