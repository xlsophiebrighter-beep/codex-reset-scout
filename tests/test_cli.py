from __future__ import annotations

import json
from datetime import UTC, datetime

from codex_reset_scout import cli
from codex_reset_scout.models import ResetCredit


def base_config() -> dict:
    return {
        "timeout_seconds": 3,
        "sources": {},
        "local": {
            "enabled": True,
            "codex_home": None,
            "include_credits": False,
        },
        "notifications": {"webhook_url": None},
    }


def test_check_json_honors_public_only_mode(monkeypatch, capsys, tmp_path) -> None:
    captured = {}

    def fake_run(config, *, state_path):
        captured["config"] = config
        captured["state_path"] = state_path
        return {
            "checked_at": "2026-07-15T04:00:00+00:00",
            "new_alerts": [],
            "alerts": [],
            "source_health": [],
            "local": {"enabled": False, "usage": None},
            "decision": {"action": "continue_normally", "urgency": "low", "reason": "none"},
            "errors": [],
        }

    monkeypatch.setattr(cli, "load_config", lambda _path=None: base_config())
    monkeypatch.setattr(cli, "run_check", fake_run)
    state = tmp_path / "state.json"

    result = cli.main(["check", "--json", "--no-local", "--state", str(state)])

    assert result == 0
    assert captured["config"]["local"]["enabled"] is False
    assert captured["state_path"] == state
    assert json.loads(capsys.readouterr().out)["new_alerts"] == []


def test_credits_command_is_explicit_and_sanitized(monkeypatch, capsys) -> None:
    captured = {}

    def fake_query(**kwargs):
        captured.update(kwargs)
        return [
            ResetCredit(
                status="available",
                reset_type="weekly",
                expires_at=datetime(2026, 7, 20, tzinfo=UTC),
            )
        ]

    monkeypatch.setattr(cli, "load_config", lambda _path=None: base_config())
    monkeypatch.setattr(cli, "query_reset_credits", fake_query)

    result = cli.main(["credits", "--json"])
    report = json.loads(capsys.readouterr().out)

    assert result == 0
    assert captured["enabled"] is True
    assert report["available_count"] == 1
    rendered = json.dumps(report)
    assert "access_token" not in rendered
    assert "account_id" not in rendered


def test_doctor_redacts_paths_unless_explicitly_requested() -> None:
    redacted = cli._doctor(base_config())

    assert redacted["paths_redacted"] is True
    assert redacted["config_path"] == "<redacted>/config.json"
    assert redacted["state_path"] == "<redacted>/state.json"


def test_cli_does_not_echo_sensitive_os_error_text(monkeypatch, capsys) -> None:
    secret_path = "C:/Users/private-name/secret-config.json"

    def fail(_path=None):
        raise OSError(f"could not read {secret_path}")

    monkeypatch.setattr(cli, "load_config", fail)

    result = cli.main(["doctor"])
    error = capsys.readouterr().err

    assert result == 1
    assert secret_path not in error
    assert "local file operation failed" in error
