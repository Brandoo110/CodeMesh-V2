"""Read-only CLI for persisted local Assurance evidence."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .local_entry import LocalAssuranceEntry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m assurance.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("gate", "passport"):
        command = commands.add_parser(name)
        command.add_argument("--database", required=True)
        command.add_argument("--artifact-root", required=True)
        command.add_argument("--workspace-root", required=True)
        command.add_argument("--case-id", required=True)
        if name == "gate":
            command.add_argument("--json", action="store_true", dest="as_json")
        else:
            command.add_argument(
                "--format", default="markdown"
            )
    return parser


def _gate_payload(projection: dict[str, object]) -> dict[str, object]:
    """Select stable, path-free facts from the authoritative projection."""

    return {
        "case_id": projection["case_id"],
        "gate": projection["gate"],
        "freshness": projection.get("freshness"),
        "allowed_actions": projection["allowed_actions"],
    }


def _gate(args: argparse.Namespace) -> int:
    with LocalAssuranceEntry(
        args.database, args.artifact_root, args.workspace_root
    ) as entry:
        payload = _gate_payload(entry.gate(args.case_id))
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        freshness = payload["freshness"] or {}
        print(f"case_id: {payload['case_id']}")
        print(f"gate: {payload['gate']}")
        print(f"freshness: {freshness.get('status', 'UNAVAILABLE')}")
        actions = payload["allowed_actions"]
        print("allowed_actions: " + ", ".join(item["code"] for item in actions))
    return 0 if payload["gate"] == "ACCEPTED" and _freshness_is_fresh(payload) else 2


def _freshness_is_fresh(payload: dict[str, object]) -> bool:
    freshness = payload.get("freshness")
    return isinstance(freshness, dict) and freshness.get("status") == "FRESH"


def _passport(args: argparse.Namespace) -> int:
    if args.format not in {"markdown", "json"}:
        raise ValueError("unsupported passport format")
    with LocalAssuranceEntry(
        args.database, args.artifact_root, args.workspace_root
    ) as entry:
        value = entry.passport(args.case_id, format=args.format)
    if args.format == "json":
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        print(value, end="\n" if not value.endswith("\n") else "")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local adapter and return its process exit status."""

    try:
        args = _parser().parse_args(argv)
        if args.command == "gate":
            return _gate(args)
        return _passport(args)
    except SystemExit as exc:
        # argparse uses SystemExit for both --help and malformed invocation.
        # Keep the adapter's contract: valid help is 0; all other CLI setup
        # errors are the generic configuration/error status 1.
        return int(exc.code or 0)
    except Exception:
        print("assurance command failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
