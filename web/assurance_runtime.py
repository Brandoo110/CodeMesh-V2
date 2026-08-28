"""Environment-gated product composition for the Assurance Run runtime.

This module is deliberately the only place where the product runtime reads its
configuration and reviewer secret.  Configuration is local, strict JSON; the
reviewer endpoint is fixed in code and can be replaced in tests only through an
explicit ``httpx`` transport.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from pydantic import SecretStr

from assurance.artifacts import ArtifactStore
from assurance.commands import CommandSpec
from assurance.fixed_reviewer_invoker import (
    FixedOpenAICompatibleReviewerInvoker,
    FixedReviewerEndpoint,
)
from assurance.live_freshness import LiveFreshnessChecker
from assurance.reviewer_context import SafeReviewerContextBuilder
from assurance.run_service import (
    AssuranceRunConfig,
    AssuranceRunService,
    ReviewerRoute,
)
from web.assurance_store import AssuranceWebRepository


_CONFIG_ENV = "CODEMESH_ASSURANCE_CONFIG"
_API_KEY_ENV = "CODEMESH_ASSURANCE_REVIEWER_API_KEY"
_FIXED_REVIEWER_BASE_URL = "https://api.deepseek.com/v1"
_STARTUP_ERROR_MESSAGE = "assurance runtime startup failed"

_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "workspace_root",
        "database_path",
        "artifact_store_root",
        "allowed_commands",
        "orchestration_version",
        "redaction_policy_version",
        "policy_version",
        "rubric_version",
        "freshness_ttl_seconds",
        "reviewer",
    }
)
_REVIEWER_KEYS = frozenset(
    {
        "provider",
        "model_ref",
        "timeout_seconds",
        "token_budget",
        "routing_rule",
    }
)
_COMMAND_KEYS = frozenset(
    {
        "schema_version",
        "command_id",
        "kind",
        "argv",
        "cwd",
        "timeout_seconds",
        "max_output_bytes",
    }
)
_REQUIRED_COMMAND_KEYS = _COMMAND_KEYS - {"schema_version"}
_EXPANSION_RE = re.compile(r"(?:[$~{}]|%[^%]+%)")


class _InvalidRuntimeConfig(ValueError):
    """Internal marker for an environment/configuration that disables runtime."""


class _PathAccessError(OSError):
    """Internal marker for an inaccessible configured path."""


class AssuranceRuntimeStartupError(RuntimeError):
    """A configured runtime failed to start without exposing failure details."""

    def __init__(self) -> None:
        super().__init__(_STARTUP_ERROR_MESSAGE)


@dataclass(frozen=True)
class AssuranceRuntimeConfig:
    """Validated local configuration, excluding the reviewer API secret."""

    schema_version: Literal["v1"]
    workspace_root: Path
    database_path: Path
    artifact_store_root: Path
    allowed_commands: tuple[CommandSpec, ...]
    orchestration_version: str
    redaction_policy_version: str
    policy_version: str
    rubric_version: str
    freshness_ttl_seconds: int
    reviewer: ReviewerRoute


@dataclass
class AssuranceRuntime:
    """The one product-owned Assurance composition and its close boundary."""

    config: AssuranceRuntimeConfig
    service_config: AssuranceRunConfig
    freshness_checker: LiveFreshnessChecker
    repository: AssuranceWebRepository
    artifact_store: ArtifactStore
    context_builder: SafeReviewerContextBuilder
    reviewer_invoker: FixedOpenAICompatibleReviewerInvoker
    service: AssuranceRunService
    _closed: bool = field(default=False, init=False, repr=False)

    async def aclose(self) -> None:
        """Close the reviewer client at most once."""

        if self._closed:
            return
        self._closed = True
        await self.reviewer_invoker.aclose()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidRuntimeConfig("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise _InvalidRuntimeConfig("non-finite JSON number")


def _strict_json(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise _InvalidRuntimeConfig("invalid strict JSON") from None


def _mapping(value: object, *, keys: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        raise _InvalidRuntimeConfig(f"{label} has an invalid shape")
    return value


def _text(value: object, *, label: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise _InvalidRuntimeConfig(f"{label} must be a nonblank string")
    return value


def _positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise _InvalidRuntimeConfig(f"{label} must be a positive integer")
    return value


def _absolute_path(value: object, *, label: str) -> Path:
    text = _text(value, label=label)
    if _EXPANSION_RE.search(text) is not None:
        raise _InvalidRuntimeConfig(f"{label} must not use path expansion")
    try:
        path = Path(text)
    except (TypeError, ValueError):
        raise _InvalidRuntimeConfig(f"{label} is invalid") from None
    if not path.is_absolute():
        raise _InvalidRuntimeConfig(f"{label} must be absolute")
    return path


def _command_specs(value: object) -> tuple[CommandSpec, ...]:
    if type(value) is not list or not value or len(value) > 16:
        raise _InvalidRuntimeConfig("allowed_commands has an invalid shape")
    result: list[CommandSpec] = []
    for item in value:
        if type(item) is not dict:
            raise _InvalidRuntimeConfig("allowed command has an invalid shape")
        command_keys = frozenset(item)
        if not _REQUIRED_COMMAND_KEYS <= command_keys <= _COMMAND_KEYS:
            raise _InvalidRuntimeConfig("allowed command has an invalid shape")
        command = dict(item)
        if command.get("schema_version", "v1") != "v1":
            raise _InvalidRuntimeConfig("allowed command schema_version is invalid")
        if "schema_version" not in command:
            command = {"schema_version": "v1", **command}
        try:
            result.append(CommandSpec.model_validate(command))
        except (TypeError, ValueError):
            raise _InvalidRuntimeConfig("allowed command is invalid") from None
    if len({item.command_id for item in result}) != len(result):
        raise _InvalidRuntimeConfig("allowed command IDs must be unique")
    return tuple(result)


def _reviewer_route(value: object) -> ReviewerRoute:
    reviewer = _mapping(value, keys=_REVIEWER_KEYS, label="reviewer")
    provider = _text(reviewer["provider"], label="reviewer.provider")
    if provider != "deepseek":
        raise _InvalidRuntimeConfig("reviewer.provider is not supported")
    model_ref = _text(reviewer["model_ref"], label="reviewer.model_ref")
    timeout_seconds = _positive_int(
        reviewer["timeout_seconds"], label="reviewer.timeout_seconds"
    )
    token_budget = _positive_int(
        reviewer["token_budget"], label="reviewer.token_budget"
    )
    routing_rule = _text(reviewer["routing_rule"], label="reviewer.routing_rule")
    try:
        return ReviewerRoute(
            provider=provider,
            model_ref=model_ref,
            timeout_seconds=timeout_seconds,
            token_budget=token_budget,
            routing_rule=routing_rule,
        )
    except (TypeError, ValueError):
        raise _InvalidRuntimeConfig("reviewer route is invalid") from None


def _parse_config(value: object) -> AssuranceRuntimeConfig:
    config = _mapping(value, keys=_CONFIG_KEYS, label="configuration")
    if config["schema_version"] != "v1":
        raise _InvalidRuntimeConfig("schema_version is invalid")
    workspace = _absolute_path(config["workspace_root"], label="workspace_root")
    database = _absolute_path(config["database_path"], label="database_path")
    artifact_root = _absolute_path(
        config["artifact_store_root"], label="artifact_store_root"
    )
    commands = _command_specs(config["allowed_commands"])
    versions = {
        name: _text(config[name], label=name)
        for name in (
            "orchestration_version",
            "redaction_policy_version",
            "policy_version",
            "rubric_version",
        )
    }
    freshness_ttl_seconds = _positive_int(
        config["freshness_ttl_seconds"], label="freshness_ttl_seconds"
    )
    return AssuranceRuntimeConfig(
        schema_version="v1",
        workspace_root=workspace,
        database_path=database,
        artifact_store_root=artifact_root,
        allowed_commands=commands,
        orchestration_version=versions["orchestration_version"],
        redaction_policy_version=versions["redaction_policy_version"],
        policy_version=versions["policy_version"],
        rubric_version=versions["rubric_version"],
        freshness_ttl_seconds=freshness_ttl_seconds,
        reviewer=_reviewer_route(config["reviewer"]),
    )


def _walk_real_components(path: Path, *, allow_missing_leaf: bool = False) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and current == path:
                return
            raise _InvalidRuntimeConfig("configured path does not exist") from None
        except PermissionError as exc:
            raise _PathAccessError from exc
        except OSError as exc:
            raise _InvalidRuntimeConfig("configured path cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise _InvalidRuntimeConfig("configured path contains a symlink")


def _real_directory(path: Path, *, label: str) -> Path:
    _walk_real_components(path)
    try:
        info = path.lstat()
    except PermissionError as exc:
        raise _PathAccessError from exc
    except OSError:
        raise _InvalidRuntimeConfig(f"{label} cannot be inspected") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _InvalidRuntimeConfig(f"{label} must be a real directory")
    try:
        return path.resolve(strict=True)
    except PermissionError as exc:
        raise _PathAccessError from exc
    except OSError:
        raise _InvalidRuntimeConfig(f"{label} cannot be resolved") from None


def _database_path(path: Path) -> Path:
    _real_directory(path.parent, label="database parent")
    _walk_real_components(path, allow_missing_leaf=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    except PermissionError as exc:
        raise _PathAccessError from exc
    except OSError:
        raise _InvalidRuntimeConfig("database path cannot be inspected") from None
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise _InvalidRuntimeConfig("database path must not be a directory")
    try:
        return path.resolve(strict=False)
    except PermissionError as exc:
        raise _PathAccessError from exc
    except OSError:
        raise _InvalidRuntimeConfig("database path cannot be resolved") from None


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _validate_paths(config: AssuranceRuntimeConfig) -> AssuranceRuntimeConfig:
    workspace = _real_directory(config.workspace_root, label="workspace_root")
    database = _database_path(config.database_path)
    artifact_root = _real_directory(
        config.artifact_store_root, label="artifact_store_root"
    )
    if _overlaps(database, workspace) or _overlaps(artifact_root, workspace):
        raise _InvalidRuntimeConfig("runtime storage must be outside workspace")
    if _overlaps(database, artifact_root):
        raise _InvalidRuntimeConfig("runtime storage paths must be separate")
    return AssuranceRuntimeConfig(
        schema_version=config.schema_version,
        workspace_root=workspace,
        database_path=database,
        artifact_store_root=artifact_root,
        allowed_commands=config.allowed_commands,
        orchestration_version=config.orchestration_version,
        redaction_policy_version=config.redaction_policy_version,
        policy_version=config.policy_version,
        rubric_version=config.rubric_version,
        freshness_ttl_seconds=config.freshness_ttl_seconds,
        reviewer=config.reviewer,
    )


def _read_config(config_ref: object) -> AssuranceRuntimeConfig | None:
    if type(config_ref) is not str or not config_ref.strip():
        return None
    try:
        path = _absolute_path(config_ref, label="configuration path")
        _walk_real_components(path, allow_missing_leaf=True)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        return _parse_config(_strict_json(path.read_bytes()))
    except (_InvalidRuntimeConfig, _PathAccessError, OSError, ValueError):
        return None


def _environment_values(environ: Mapping[str, str] | None) -> tuple[object, object] | None:
    source = os.environ if environ is None else environ
    if not isinstance(source, Mapping):
        return None
    try:
        config_ref = source.get(_CONFIG_ENV)
        api_key = source.get(_API_KEY_ENV)
    except Exception:
        return None
    if type(config_ref) is not str or not config_ref.strip():
        return None
    if type(api_key) is not str or not api_key.strip():
        return None
    return config_ref, api_key


def _build_runtime(
    config: AssuranceRuntimeConfig,
    api_key: str,
    reviewer_transport: httpx.AsyncBaseTransport | None,
) -> AssuranceRuntime:
    try:
        if reviewer_transport is not None and not isinstance(
            reviewer_transport, httpx.AsyncBaseTransport
        ):
            raise TypeError("reviewer_transport must be an async transport")
        service_config = AssuranceRunConfig(
            workspace_root=config.workspace_root,
            allowed_commands=config.allowed_commands,
            orchestration_version=config.orchestration_version,
            redaction_policy_version=config.redaction_policy_version,
            policy_version=config.policy_version,
            rubric_version=config.rubric_version,
            freshness_ttl_seconds=config.freshness_ttl_seconds,
            reviewer_route=config.reviewer,
        )
        freshness_checker = LiveFreshnessChecker(
            workspace_root=config.workspace_root
        )
        repository = AssuranceWebRepository(
            config.database_path,
            freshness_checker=freshness_checker,
            live_required=True,
        )
        repository.initialize()
        artifact_store = ArtifactStore(config.artifact_store_root)
        context_builder = SafeReviewerContextBuilder()
        endpoint = FixedReviewerEndpoint(
            route=config.reviewer,
            base_url=_FIXED_REVIEWER_BASE_URL,
            api_key=SecretStr(api_key),
        )
        reviewer_invoker = FixedOpenAICompatibleReviewerInvoker(
            endpoint,
            transport=reviewer_transport,
        )
        service = AssuranceRunService(
            artifact_store=artifact_store,
            reviewer_invoker=reviewer_invoker,
            committer=repository,
            context_builder=context_builder,
            config=service_config,
        )
    except Exception:
        raise AssuranceRuntimeStartupError() from None
    return AssuranceRuntime(
        config=config,
        service_config=service_config,
        freshness_checker=freshness_checker,
        repository=repository,
        artifact_store=artifact_store,
        context_builder=context_builder,
        reviewer_invoker=reviewer_invoker,
        service=service,
    )


def load_assurance_runtime_from_environment(
    environ: Mapping[str, str] | None = None,
    reviewer_transport: httpx.AsyncBaseTransport | None = None,
) -> AssuranceRuntime | None:
    """Load the product runtime, or return ``None`` while it is disabled.

    Missing/blank environment values and invalid local configuration disable the
    optional runtime before any repository, client, source, or command object
    is initialized.  A structurally valid configuration whose owned resources
    fail during construction raises only the fixed startup error.
    """

    values = _environment_values(environ)
    if values is None:
        return None
    config_ref, api_key = values
    config = _read_config(config_ref)
    if config is None:
        return None
    try:
        config = _validate_paths(config)
    except _InvalidRuntimeConfig:
        return None
    except _PathAccessError:
        raise AssuranceRuntimeStartupError() from None
    return _build_runtime(config, api_key, reviewer_transport)


__all__ = [
    "AssuranceRuntime",
    "AssuranceRuntimeConfig",
    "AssuranceRuntimeStartupError",
    "load_assurance_runtime_from_environment",
]
