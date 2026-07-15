from __future__ import annotations

import json

from codex_reset_scout.config import load_config


def test_load_config_deep_merges_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"sources": {"reddit": False}, "lookback_hours": 24}),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config["lookback_hours"] == 24
    assert config["sources"]["reddit"] is False
    assert config["sources"]["openai_status"] is True
