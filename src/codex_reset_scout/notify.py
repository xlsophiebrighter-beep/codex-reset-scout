from __future__ import annotations

import json
import urllib.request
from typing import Any


def post_webhook(url: str, payload: dict[str, Any], timeout: int = 10) -> None:
    """Post a sanitized JSON alert to a user-configured webhook."""

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "codex-reset-scout/0.1"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status >= 400:
            raise RuntimeError(f"webhook returned HTTP {response.status}")
