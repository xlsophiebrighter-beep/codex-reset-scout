from __future__ import annotations

import base64
import json
import math
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG
from .local_usage import default_codex_home
from .models import ResetCredit

RESET_CREDITS_ENDPOINT = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
_AUTH_CLAIM = "https://api.openai.com/auth"
_MAX_AUTH_BYTES = 5 * 1024 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024


class CreditsOptInRequired(PermissionError):
    """Raised before auth is read when the experimental probe was not enabled."""


class CreditQueryError(RuntimeError):
    """A sanitized local-auth or network failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _pick(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        candidate = value.get(key)
        if candidate is not None:
            return candidate
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
            if not math.isfinite(number):
                return None
            if abs(number) > 10_000_000_000:
                number /= 1000
            parsed = datetime.fromtimestamp(number, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is not None:
            return _parse_datetime(number)
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_text(value: Any, default: str) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return default
    text = str(value).strip()
    return text[:100] if text else default


def _read_auth(auth_path: Path) -> Mapping[str, Any]:
    try:
        with auth_path.open("rb") as stream:
            body = stream.read(_MAX_AUTH_BYTES + 1)
    except OSError:
        raise CreditQueryError("Codex authentication is unavailable") from None
    if len(body) > _MAX_AUTH_BYTES:
        raise CreditQueryError("Codex authentication file is unexpectedly large")
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise CreditQueryError("Codex authentication has an unsupported format") from None
    if not isinstance(data, Mapping):
        raise CreditQueryError("Codex authentication has an unsupported format")
    return data


def _decode_account_id(access_token: str) -> str | None:
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
        auth_claim = _mapping(_mapping(payload).get(_AUTH_CLAIM))
        account_id = _pick(auth_claim, "chatgpt_account_id", "chatgptAccountId")
        return str(account_id) if account_id else None
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _credentials(auth: Mapping[str, Any]) -> tuple[str, str | None]:
    tokens = _mapping(auth.get("tokens"))
    source = tokens or auth
    access_token = _pick(source, "access_token", "accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise CreditQueryError("Codex authentication does not contain an access token")
    account_id = _pick(source, "account_id", "accountId")
    if not account_id:
        account_id = _decode_account_id(access_token)
    return access_token, str(account_id) if account_id else None


def _request_body(access_token: str, account_id: str | None, timeout: int, opener: Any) -> bytes:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "OAI-Product-Sku": "CODEX",
        "User-Agent": "codex-reset-scout/0.1",
        "originator": "Codex Desktop",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = urllib.request.Request(RESET_CREDITS_ENDPOINT, headers=headers, method="GET")
    client = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with client.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is not None and int(status) >= 400:
                raise CreditQueryError(f"Reset-credit query returned HTTP {int(status)}")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except CreditQueryError:
        raise
    except urllib.error.HTTPError as error:
        raise CreditQueryError(f"Reset-credit query returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CreditQueryError("Reset-credit query failed") from None
    if len(body) > _MAX_RESPONSE_BYTES:
        raise CreditQueryError("Reset-credit response is unexpectedly large")
    return body


def _credit_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    data = _mapping(payload)
    for key in ("credits", "reset_credits", "resetCredits", "items"):
        candidate = data.get(key)
        if isinstance(candidate, list):
            return candidate
        nested_items = _mapping(candidate).get("items")
        if isinstance(nested_items, list):
            return nested_items
    nested = data.get("data")
    return _credit_items(nested) if isinstance(nested, (Mapping, list)) else []


def _parse_credits(payload: Any) -> list[ResetCredit]:
    credits: list[ResetCredit] = []
    for value in _credit_items(payload):
        item = _mapping(value)
        if not item:
            continue
        known_value = _pick(
            item,
            "status",
            "state",
            "reset_type",
            "resetType",
            "granted_at",
            "grantedAt",
            "expires_at",
            "expiresAt",
        )
        if known_value is None:
            continue
        credits.append(
            ResetCredit(
                status=_safe_text(_pick(item, "status", "state"), "unknown"),
                reset_type=_safe_text(_pick(item, "reset_type", "resetType", "type"), "unknown"),
                granted_at=_parse_datetime(
                    _pick(item, "granted_at", "grantedAt", "created_at", "createdAt")
                ),
                expires_at=_parse_datetime(
                    _pick(item, "expires_at", "expiresAt", "expiration", "expiry")
                ),
            )
        )
    return sorted(
        credits,
        key=lambda credit: (
            credit.expires_at is None,
            credit.expires_at or datetime.max.replace(tzinfo=UTC),
            credit.status,
            credit.reset_type,
        ),
    )


def query_reset_credits(
    *,
    enabled: bool = False,
    codex_home: str | Path | None = None,
    timeout: int = 15,
    opener: Any = None,
) -> list[ResetCredit]:
    """Query the undocumented read-only reset-credit endpoint after explicit opt-in.

    Authentication and the raw response remain in memory and are never returned. The endpoint is
    experimental, unsupported, and may change without notice.
    """

    if enabled is not True:
        raise CreditsOptInRequired("Experimental reset-credit access requires explicit opt-in")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("timeout must be a positive integer")
    home = Path(codex_home).expanduser() if codex_home else default_codex_home()
    auth = _read_auth(home / "auth.json")
    access_token, account_id = _credentials(auth)
    body = _request_body(access_token, account_id, timeout, opener)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise CreditQueryError("Reset-credit response has an unsupported format") from None
    return _parse_credits(payload)


def configured_reset_credits(config: Mapping[str, Any], opener: Any = None) -> list[ResetCredit]:
    """Use the existing config's include_credits flag as the explicit opt-in."""

    local = _mapping(config.get("local"))
    if local.get("include_credits", DEFAULT_CONFIG["local"]["include_credits"]) is not True:
        return []
    timeout = config.get("timeout_seconds", DEFAULT_CONFIG["timeout_seconds"])
    return query_reset_credits(
        enabled=True,
        codex_home=local.get("codex_home"),
        timeout=int(timeout),
        opener=opener,
    )
