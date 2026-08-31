"""Read-only CLI for persisted local Assurance evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import typer

from .entry import AssuranceHttpClient, AssuranceRunReadback
from .integrations.github_client import GitHubCheckPublisher
from .local_entry import LocalAssuranceEntry


app = typer.Typer(
    name="assurance",
    help="CodeMesh Change Assurance 的真实本地入口。",
    no_args_is_help=True,
)
github_app = typer.Typer(
    name="github",
    help="将权威 Assurance Passport 发布为 GitHub Check。",
    no_args_is_help=True,
)
app.add_typer(github_app, name="github")


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


# ---------------------------------------------------------------------------
# Live Golden Path entry
# ---------------------------------------------------------------------------

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DEFAULT_TASK_PATH = "examples/quickstart-task.md"
_DEFAULT_COMMAND_ID = "diff-check"
_DEFAULT_ASSURANCE_API = "http://127.0.0.1:8010"
_DEFAULT_WORKBENCH_URL = "http://127.0.0.1:3010/?view=assurance"


def _git_output(repository: Path, *arguments: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _repository_head(repository: Path) -> str:
    output = _git_output(repository, "rev-parse", "--verify", "HEAD^{commit}")
    if output is None:
        raise ValueError("repository HEAD could not be read")
    try:
        value = output.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("repository HEAD is invalid") from exc
    if value.endswith("\n"):
        value = value[:-1]
    if _GIT_OBJECT_ID_RE.fullmatch(value) is None:
        raise ValueError("repository HEAD is invalid")
    return value


def _repository_path(value: Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ValueError("repository must be an existing directory")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("repository must be an existing directory") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("repository must be an existing directory")
    return resolved


def _relative_document(repository: Path, value: Path | str) -> str:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise ValueError("declared document path must not contain parent segments")
    candidate = raw if raw.is_absolute() else repository / raw
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(repository)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("declared document must be inside repository") from exc
    path = relative.as_posix()
    if not path or path.startswith("../") or not path.endswith(".md"):
        raise ValueError("declared document must be a canonical Markdown path")
    if not resolved.is_file():
        raise ValueError("declared document must be a file")
    return path


def _derive_repository_identity(repository: Path) -> str:
    candidate = (os.getenv("GITHUB_REPOSITORY") or "").strip()
    if candidate and re.fullmatch(r"[^/\s?#]+/[^/\s?#]+", candidate):
        return candidate

    remote_bytes = _git_output(repository, "config", "--get", "remote.origin.url")
    remote = remote_bytes.decode("utf-8", "replace").strip() if remote_bytes else ""
    if remote:
        parsed = urlsplit(remote)
        if parsed.hostname and parsed.path.strip("/"):
            path = parsed.path.strip("/").removesuffix(".git")
            if path:
                return f"{parsed.hostname}/{path}"
        scp_match = re.match(r"^[^@\s/:]+@([^:\s/]+):(.+)$", remote)
        if scp_match:
            path = scp_match.group(2).strip("/").removesuffix(".git")
            if path:
                return f"{scp_match.group(1)}/{path}"

    name = repository.name.strip() or "repository"
    return f"local/{name}"


def _derive_author() -> str:
    for name in ("CODEMESH_AUTHOR", "GITHUB_ACTOR"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return "local-cli"


def _derive_base_ref(repository: Path) -> str:
    for candidate in ("origin/main", "main", "HEAD"):
        if _git_output(repository, "rev-parse", "--verify", f"{candidate}^{{commit}}"):
            return candidate
    return "HEAD"


def _path_digests(repository: Path, paths: Sequence[str]) -> list[dict[str, str]]:
    digests: list[dict[str, str]] = []
    for relative in sorted(set(paths)):
        try:
            content = (repository / relative).read_bytes()
        except OSError as exc:
            raise ValueError("declared document could not be read") from exc
        digests.append(
            {
                "path": relative,
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
    return digests


def _idempotency_key(
    repository: Path,
    request: Mapping[str, object],
    declared_paths: Sequence[str],
) -> str:
    stable_request = dict(request)
    stable_request["repository_path"] = "."
    fingerprint = {
        "schema_version": "v1",
        "request": stable_request,
        "repository_head": _repository_head(repository),
        "documents": _path_digests(repository, declared_paths),
        "worktree": "sha256:"
        + hashlib.sha256(
            _git_output(
                repository,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            )
            or b""
        ).hexdigest(),
    }
    encoded = json.dumps(
        fingerprint,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "codemesh-" + hashlib.sha256(encoded).hexdigest()


def _request_value(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _public_run_payload(result: AssuranceRunReadback | Mapping[str, object], workbench_url: str) -> dict[str, object]:
    case_view = _request_value(result, "case_view")
    if not isinstance(case_view, Mapping):
        raise ValueError("authoritative CaseView was invalid")
    case_id = _request_value(result, "case_id")
    run_id = _request_value(result, "run_id")
    request_digest = _request_value(result, "request_digest")
    cached = _request_value(result, "cached")
    subject_digest = case_view.get("subject_digest")
    raw_gate = case_view.get("policy_gate", case_view.get("gate"))
    gate = (
        raw_gate.get("status")
        if isinstance(raw_gate, Mapping)
        else raw_gate
    )
    case_state = case_view.get("acceptance_state")
    freshness = case_view.get("freshness")
    actions = case_view.get("allowed_actions", [])
    if (
        type(case_id) is not str
        or type(run_id) is not str
        or type(request_digest) is not str
        or type(cached) is not bool
        or type(subject_digest) is not str
        or type(gate) is not str
        or type(case_state) is not str
        or not isinstance(freshness, Mapping)
        or not isinstance(actions, list)
    ):
        raise ValueError("authoritative CaseView was incomplete")
    action_codes: list[str] = []
    for item in actions:
        if not isinstance(item, Mapping) or type(item.get("code")) is not str:
            raise ValueError("authoritative allowed actions were invalid")
        action_codes.append(item["code"])
    return {
        "schema_version": "v1",
        "run_id": run_id,
        "request_digest": request_digest,
        "cached": cached,
        "case_id": case_id,
        "subject_digest": subject_digest,
        "gate": gate,
        "state": case_state,
        "freshness": dict(freshness),
        "allowed_actions": action_codes,
        "workbench_url": workbench_url,
    }


def _workbench_url(value: str | None) -> str:
    raw = (value or os.getenv("CODEMESH_WORKBENCH_URL") or _DEFAULT_WORKBENCH_URL).strip()
    try:
        parsed = httpx.URL(raw)
    except Exception as exc:  # pragma: no cover - parser boundary
        raise ValueError("workbench URL must be an HTTP URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("workbench URL must be an HTTP URL")
    if parsed.username or parsed.password:
        raise ValueError("workbench URL must not contain credentials")
    if any(marker in raw.lower() for marker in ("token", "secret", "password", "api_key")):
        raise ValueError("workbench URL contains a forbidden credential marker")
    return raw


def _perform_live_run(
    *,
    repository: Path,
    repository_identity: str,
    author: str,
    base_ref: str,
    task_path: Path | str,
    policy_paths: Sequence[str],
    adr_paths: Sequence[str],
    runbook_paths: Sequence[str],
    command_ids: Sequence[str],
    official_evidence_run_id: str | None = None,
    changed_lines_total: int | None,
    external_side_effects: str,
    provider_boundary: str,
    idempotency_key: str | None,
    api_url: str,
) -> AssuranceRunReadback:
    root = _repository_path(repository)
    task_relative = _relative_document(root, task_path)
    policy_relative = tuple(_relative_document(root, item) for item in policy_paths)
    adr_relative = tuple(_relative_document(root, item) for item in adr_paths)
    runbook_relative = tuple(_relative_document(root, item) for item in runbook_paths)
    if official_evidence_run_id is not None:
        if (
            type(official_evidence_run_id) is not str
            or not official_evidence_run_id.isascii()
            or not official_evidence_run_id.isdecimal()
            or official_evidence_run_id == "0"
            or official_evidence_run_id.startswith("0")
            or len(official_evidence_run_id) > 19
        ):
            raise ValueError("official evidence run id must be positive numeric text")
    commands = tuple(command_ids)
    if not 1 <= len(commands) <= 16 or any(
        type(item) is not str or not item.strip() for item in commands
    ):
        raise ValueError("at least one allowed command id is required")
    if len(set(commands)) != len(commands):
        raise ValueError("command ids must be unique")
    if external_side_effects not in {"none_declared", "present_declared", "unknown"}:
        raise ValueError("external side-effects declaration is invalid")
    if provider_boundary not in {
        "within_declared_boundary",
        "crosses_declared_boundary",
        "unknown",
    }:
        raise ValueError("provider boundary declaration is invalid")

    request: dict[str, object] = {
        "repository_path": str(root),
        "repository_identity": _derive_repository_identity(root)
        if not repository_identity
        else repository_identity,
        "author": author or _derive_author(),
        "base_ref": base_ref,
        "task_path": task_relative,
        "policy_paths": policy_relative,
        "adr_paths": adr_relative,
        "runbook_paths": runbook_relative,
        "command_ids": commands,
        "official_evidence_run_id": official_evidence_run_id,
        "changed_lines_total": changed_lines_total,
        "external_side_effects": external_side_effects,
        "provider_boundary": provider_boundary,
    }
    key = (
        idempotency_key
        if idempotency_key is not None
        else _idempotency_key(
            root,
            request,
            (task_relative, *policy_relative, *adr_relative, *runbook_relative),
        )
    )
    with AssuranceHttpClient(api_url) as client:
        return client.run_and_readback(request, idempotency_key=key)


@app.command("run")
def run_entry(
    repository: Path = typer.Option(Path("."), "--repository", "-r", help="待验收的 Git 仓库"),
    repository_identity: str | None = typer.Option(None, "--repository-identity", help="逻辑仓库身份，例如 owner/repo"),
    author: str | None = typer.Option(None, "--author", help="作者标识"),
    base_ref: str | None = typer.Option(None, "--base-ref", help="Git 基线 ref，默认 origin/main/main/HEAD"),
    task_path: Path = typer.Option(Path(_DEFAULT_TASK_PATH), "--task-path", help="仓库内 Markdown 任务说明"),
    policy_path: list[str] | None = typer.Option(None, "--policy-path", help="仓库内 Markdown policy，可重复"),
    adr_path: list[str] | None = typer.Option(None, "--adr-path", help="仓库内 Markdown ADR，可重复"),
    runbook_path: list[str] | None = typer.Option(None, "--runbook-path", help="仓库内 Markdown runbook，可重复"),
    command_id: list[str] | None = typer.Option(None, "--command-id", help="runtime allowlist 中的 command id，可重复"),
    official_evidence_run_id: str | None = typer.Option(
        None,
        "--official-evidence-run-id",
        help="单一已完成且成功的 P-C GitHub Actions run id",
    ),
    changed_lines_total: int | None = typer.Option(None, "--changed-lines-total", min=0),
    external_side_effects: str = typer.Option("unknown", "--external-side-effects"),
    provider_boundary: str = typer.Option("within_declared_boundary", "--provider-boundary"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key", help="可选；默认按当前输入与工作树稳定生成"),
    api_url: str | None = typer.Option(None, "--api-url", help="本地 Assurance API，默认 127.0.0.1:8010"),
    workbench_url: str | None = typer.Option(None, "--workbench-url", help="Workbench 地址"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """从仓库、任务、policy 和 allowlisted commands 创建真实 Assurance Run。"""

    try:
        root = _repository_path(repository)
        identity = repository_identity or _derive_repository_identity(root)
        effective_base = base_ref or _derive_base_ref(root)
        result = _perform_live_run(
            repository=root,
            repository_identity=identity,
            author=author or _derive_author(),
            base_ref=effective_base,
            task_path=task_path,
            policy_paths=tuple(policy_path or ()),
            adr_paths=tuple(adr_path or ()),
            runbook_paths=tuple(runbook_path or ()),
            command_ids=tuple(command_id or (_DEFAULT_COMMAND_ID,)),
            official_evidence_run_id=official_evidence_run_id,
            changed_lines_total=changed_lines_total,
            external_side_effects=external_side_effects,
            provider_boundary=provider_boundary,
            idempotency_key=idempotency_key,
            api_url=api_url or os.getenv("CODEMESH_ASSURANCE_API_URL", _DEFAULT_ASSURANCE_API),
        )
        payload = _public_run_payload(result, _workbench_url(workbench_url))
    except Exception:
        typer.echo("assurance run failed; no authoritative CaseView was accepted", err=True)
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    typer.echo(f"run_id: {payload['run_id']}")
    typer.echo(f"case_id: {payload['case_id']}")
    typer.echo(f"subject: {payload['subject_digest']}")
    typer.echo(f"state: {payload['state']}")
    typer.echo(f"gate: {payload['gate']}")
    freshness = payload["freshness"]
    typer.echo(f"freshness: {freshness.get('status', 'UNAVAILABLE')}")
    actions = payload["allowed_actions"]
    typer.echo("allowed_actions: " + (", ".join(actions) if actions else "none"))
    typer.echo(f"workbench: {payload['workbench_url']}")
    if payload["cached"]:
        typer.echo("replay: cached authoritative run")


@github_app.command("publish")
def publish_github_check(
    case_id: str = typer.Option(..., "--case-id", help="要发布的 Case ID"),
    owner: str | None = typer.Option(None, "--owner", help="GitHub owner；默认 GITHUB_REPOSITORY"),
    repo: str | None = typer.Option(None, "--repo", help="GitHub repo；默认 GITHUB_REPOSITORY"),
    head_sha: str | None = typer.Option(None, "--head-sha", help="目标 commit SHA；默认 GITHUB_SHA"),
    token_env: str = typer.Option("GITHUB_TOKEN", "--token-env", help="读取 GitHub token 的环境变量名"),
    assurance_api_url: str | None = typer.Option(None, "--assurance-api-url", help="本地 Assurance API"),
    github_api_url: str = typer.Option("https://api.github.com", "--github-api-url", help="GitHub API 地址"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """读取权威 Passport，并发布一个绑定 SHA 的 GitHub Check Run。"""

    try:
        token = (os.getenv(token_env) or "").strip()
        if not token:
            raise ValueError("GitHub token environment variable is missing")
        repository = (os.getenv("GITHUB_REPOSITORY") or "").strip()
        env_owner, separator, env_repo = repository.partition("/")
        selected_owner = owner or (env_owner if separator else "")
        selected_repo = repo or (env_repo if separator else "")
        selected_sha = head_sha or (os.getenv("GITHUB_SHA") or "").strip()
        if not selected_owner or not selected_repo or not selected_sha:
            raise ValueError("GitHub owner, repo, and head SHA are required")
        if _SHA1_RE.fullmatch(selected_sha) is None:
            raise ValueError("head SHA is invalid")
        assurance_url = assurance_api_url or os.getenv(
            "CODEMESH_ASSURANCE_API_URL", _DEFAULT_ASSURANCE_API
        )
        with AssuranceHttpClient(assurance_url) as client:
            passport = client.get_passport(case_id)
        with GitHubCheckPublisher(
            token=token,
            api_url=github_api_url,
        ) as publisher:
            result = publisher.publish(
                passport,
                owner=selected_owner,
                repo=selected_repo,
                head_sha=selected_sha,
            )
        payload = result.model_dump(mode="json")
    except Exception:
        typer.echo("GitHub Check publish failed; provider readback was not accepted", err=True)
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    typer.echo(f"check_id: {payload['check_id']}")
    typer.echo(f"case_id: {payload['case_id']}")
    typer.echo(f"subject: {payload['subject_digest']}")
    typer.echo(f"passport: {payload['passport_digest']}")
    typer.echo(f"head_sha: {payload['head_sha']}")
    typer.echo(f"conclusion: {payload['conclusion']}")
    if payload.get("check_url"):
        typer.echo(f"check: {payload['check_url']}")
    if payload["reused"]:
        typer.echo("replay: reused matching Check Run")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"run", "github"}:
        app()
    else:
        raise SystemExit(main())


__all__ = ["app", "github_app", "main"]
