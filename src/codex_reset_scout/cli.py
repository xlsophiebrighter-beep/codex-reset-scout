from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import __version__
from .config import default_config_path, load_config
from .credits import CreditQueryError, CreditsOptInRequired, query_reset_credits
from .engine import run_check
from .local_usage import latest_usage_snapshot
from .models import ResetCredit, json_ready
from .state import default_state_path


def _check_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="path to a JSON configuration file")
    parser.add_argument("--state", type=Path, help="path to the durable seen-state file")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--no-local", action="store_true", help="skip local Codex session data")
    parser.add_argument(
        "--include-credits",
        action="store_true",
        help="explicitly enable the experimental read-only reset-credit check",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-scout",
        description="Early warning and local confirmation for Codex usage resets.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="show safe configuration diagnostics")
    doctor.add_argument("--config", type=Path)
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--show-paths",
        action="store_true",
        help="include absolute local paths that are redacted by default",
    )

    check = subparsers.add_parser("check", help="run one monitoring pass")
    _check_options(check)

    watch = subparsers.add_parser("watch", help="run monitoring passes continuously")
    _check_options(watch)
    watch.add_argument("--interval", type=int, default=3600, help="seconds between checks")

    local = subparsers.add_parser("local", help="show a passive local usage snapshot")
    local.add_argument("--config", type=Path)
    local.add_argument("--json", action="store_true")

    credits = subparsers.add_parser(
        "credits", help="explicitly query experimental banked reset credits"
    )
    credits.add_argument("--config", type=Path)
    credits.add_argument("--json", action="store_true")
    return parser


def _configured(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    config = deepcopy(config)
    if getattr(args, "no_local", False):
        config["local"]["enabled"] = False
    if getattr(args, "include_credits", False):
        config["local"]["enabled"] = True
        config["local"]["include_credits"] = True
    return config


def _print_json(value: object) -> None:
    print(json.dumps(json_ready(value), ensure_ascii=False, indent=2, sort_keys=True))


def _percent(value: object) -> str:
    return "unknown" if not isinstance(value, (int, float)) else f"{float(value):.1f}%"


def _print_check(report: dict[str, Any]) -> None:
    print(f"Checked: {report['checked_at']}")
    new_alerts = report.get("new_alerts", [])
    if new_alerts:
        print(f"New alerts: {len(new_alerts)}")
        for alert in new_alerts:
            print(f"- [{alert['stage']}/{alert['confidence']}] {alert['title']}: {alert['reason']}")
            if alert.get("url"):
                print(f"  {alert['url']}")
    else:
        print("New alerts: none")

    local = report.get("local", {})
    usage = local.get("usage") if isinstance(local, dict) else None
    if isinstance(usage, dict):
        primary = usage.get("primary", {})
        secondary = usage.get("secondary", {})
        print(
            "Local usage: "
            f"primary {_percent(primary.get('used_percent'))}, "
            f"secondary {_percent(secondary.get('used_percent'))}"
        )
    decision = report.get("decision", {})
    print(
        f"Decision: {decision.get('action', 'unknown')} "
        f"({decision.get('urgency', 'unknown')}) — {decision.get('reason', '')}"
    )
    for error in report.get("errors", []):
        print(f"Warning: {error}", file=sys.stderr)


def _display_path(path: Path, show_paths: bool) -> str:
    return str(path) if show_paths else f"<redacted>/{path.name}"


def _doctor(
    config: dict[str, Any],
    *,
    show_paths: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    local = config.get("local", {})
    configured_home = local.get("codex_home")
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    sources = config.get("sources", {})
    active_config_path = config_path or default_config_path()
    return {
        "version": __version__,
        "config_path": _display_path(active_config_path, show_paths),
        "state_path": _display_path(default_state_path(), show_paths),
        "paths_redacted": not show_paths,
        "codex_home_exists": codex_home.is_dir(),
        "auth_file_present": (codex_home / "auth.json").is_file(),
        "local_enabled": bool(local.get("enabled", True)),
        "credits_enabled_by_config": bool(local.get("include_credits", False)),
        "enabled_public_sources": [
            name
            for name in ("openai_status", "developer_community", "github_issues", "reddit")
            if sources.get(name, False)
        ],
        "tibo_feed_count": len(sources.get("tibo_feed_urls", [])),
    }


def _print_doctor(report: dict[str, Any]) -> None:
    print(f"Codex Reset Scout {report['version']}")
    print(f"Configuration: {report['config_path']}")
    print(f"Seen-state: {report['state_path']}")
    print(f"Codex home available: {report['codex_home_exists']}")
    print(f"Codex auth file present: {report['auth_file_present']}")
    print(f"Passive local check enabled: {report['local_enabled']}")
    print(f"Experimental credit check enabled: {report['credits_enabled_by_config']}")
    print(f"Tibo feeds configured: {report['tibo_feed_count']}")
    print("Public sources: " + ", ".join(report["enabled_public_sources"]))


def _safe_credit_report(credits: list[ResetCredit]) -> dict[str, Any]:
    available = [credit for credit in credits if credit.status.lower() == "available"]
    return {
        "count": len(credits),
        "available_count": len(available),
        "credits": json_ready(credits),
    }


def _print_credit_report(report: dict[str, Any]) -> None:
    print(f"Reset credits: {report['available_count']} available ({report['count']} total)")
    for credit in report["credits"]:
        print(
            f"- {credit['status']} / {credit['reset_type']} / "
            f"expires {credit.get('expires_at') or 'unknown'}"
        )


def _run_once(args: argparse.Namespace) -> int:
    config = _configured(args)
    report = run_check(config, state_path=args.state)
    _print_json(report) if args.json else _print_check(report)
    return 1 if report.get("errors") else 0


def _safe_exception_message(exc: Exception) -> str:
    if isinstance(exc, (CreditQueryError, CreditsOptInRequired)):
        return str(exc)
    if isinstance(exc, json.JSONDecodeError):
        return "configuration JSON is invalid"
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, OSError):
        return "local file operation failed"
    if isinstance(exc, ValueError):
        return "invalid configuration or argument value"
    return "operation failed"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "doctor":
            report = _doctor(
                load_config(args.config),
                show_paths=args.show_paths,
                config_path=args.config,
            )
            _print_json(report) if args.json else _print_doctor(report)
            return 0

        if args.command == "check":
            return _run_once(args)

        if args.command == "watch":
            if args.interval < 30:
                parser.error("--interval must be at least 30 seconds")
            while True:
                _run_once(args)
                time.sleep(args.interval)

        if args.command == "local":
            config = load_config(args.config)
            snapshot = latest_usage_snapshot(codex_home=config.get("local", {}).get("codex_home"))
            _print_json(snapshot) if args.json else _print_json(snapshot)
            return 0

        if args.command == "credits":
            config = load_config(args.config)
            local = config.get("local", {})
            credits = query_reset_credits(
                enabled=True,
                codex_home=local.get("codex_home"),
                timeout=int(config.get("timeout_seconds", 15)),
            )
            report = _safe_credit_report(credits)
            _print_json(report) if args.json else _print_credit_report(report)
            return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, PermissionError, RuntimeError) as exc:
        print(f"Error: {type(exc).__name__}: {_safe_exception_message(exc)}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
