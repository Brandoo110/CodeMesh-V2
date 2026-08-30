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
import inspect
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import SecretStr, ValidationError

from assurance.artifacts import ArtifactStore
from assurance.commands import CommandSpec
from assurance.contracts import Finding
from assurance.digests import SubjectDigestInput, compute_subject_digest
from assurance.fixed_reviewer_invoker import (
    FixedOpenAICompatibleReviewerInvoker,
    FixedReviewerEndpoint,
)
from assurance.live_freshness import LiveFreshnessChecker
from assurance.remediation import (
    RemediationController,
    RemediationPolicy,
    RemediationRequest,
)
from assurance.store import StoreConflictError
from assurance.remediation_agent import StructuredRemediationAgent
from assurance.remediation_reviewer import AssuranceRemediationReviewer
from assurance.remediation_validation import ValidationCheck, ValidationExecutor
from assurance.remediation_workspace import IsolatedWorkspace, WorkspaceGrant
from assurance.reviewer_context import SafeReviewerContextBuilder
from assurance.run_service import (
    AssuranceRunConfig,
    AssuranceRunBundle,
    AssuranceRunService,
    FreshnessSourceBinding,
    ReviewerRoute,
)
from assurance.snapshot import GitSnapshotResult
from orchestration.adapters import (
    DashScopeAdapter,
    DeepSeekAdapter,
    GeminiAdapter,
    MiniMaxAdapter,
    VolcEngineAdapter,
)
from orchestration.adapters.base import ModelAdapter
from web.assurance_remediation import (
    AssuranceRemediationConfig,
    AssuranceRemediationError,
    AssuranceRemediationPreparationError,
    AssuranceRemediationService,
    RemediationPreparationStage,
    _preparation_reason_code,
)
from web.assurance_store import AssuranceWebError, AssuranceWebRepository
from web.assurance_store import RemediationContext


_CONFIG_ENV = "CODEMESH_ASSURANCE_CONFIG"
_API_KEY_ENV = "CODEMESH_ASSURANCE_REVIEWER_API_KEY"
_REMEDIATION_API_KEY_ENV = "CODEMESH_ASSURANCE_REMEDIATION_API_KEY"
_FIXED_REVIEWER_BASE_URL = "https://api.deepseek.com/v1"
_STARTUP_ERROR_MESSAGE = "assurance runtime startup failed"

_CONFIG_V1_KEYS = frozenset(
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
_CONFIG_V2_KEYS = _CONFIG_V1_KEYS | {"remediation"}
_CONFIG_KEYS = _CONFIG_V1_KEYS
_REVIEWER_KEYS = frozenset(
    {
        "provider",
        "model_ref",
        "timeout_seconds",
        "token_budget",
        "routing_rule",
    }
)
_REMEDIATION_KEYS = frozenset(
    {"provider", "model_ref", "workspace_grant", "policy"}
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


RemediationAdapterFactory = Callable[[str, str, str], ModelAdapter]
_REMEDIATION_PROVIDERS = frozenset(
    {"qwen", "dashscope", "gemini", "volcengine", "doubao", "minimax", "deepseek"}
)


class _InvalidRuntimeConfig(ValueError):
    """Internal marker for an environment/configuration that disables runtime."""


class _PathAccessError(OSError):
    """Internal marker for an inaccessible configured path."""


class AssuranceRuntimeStartupError(RuntimeError):
    """A configured runtime failed to start without exposing failure details."""

    def __init__(self) -> None:
        super().__init__(_STARTUP_ERROR_MESSAGE)


@dataclass(frozen=True)
class AssuranceRemediationRuntimeConfig:
    """Strict, server-owned remediation provider and policy configuration."""

    provider: str
    model_ref: str
    workspace_grant: WorkspaceGrant
    policy: RemediationPolicy


@dataclass(frozen=True)
class AssuranceRuntimeConfig:
    """Validated local configuration, excluding the reviewer API secret."""

    schema_version: Literal["v1", "v2"]
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
    remediation: AssuranceRemediationRuntimeConfig | None = None


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
    remediation_service: AssuranceRemediationService | None = None
    remediation_adapter: ModelAdapter | None = field(default=None, repr=False)
    remediation_adapter_close: Callable[[], Any] | None = field(
        default=None, repr=False
    )
    _closed: bool = field(default=False, init=False, repr=False)

    async def aclose(self) -> None:
        """Close all owned transports at most once with a fixed public error."""

        if self._closed:
            return
        self._closed = True
        close_failed = False
        try:
            await self.reviewer_invoker.aclose()
        except Exception:
            close_failed = True
        if self.remediation_adapter_close is not None:
            try:
                result = self.remediation_adapter_close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                close_failed = True
        if close_failed:
            raise AssuranceRuntimeStartupError() from None


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


def _remediation_runtime_config(value: object) -> AssuranceRemediationRuntimeConfig:
    remediation = _mapping(value, keys=_REMEDIATION_KEYS, label="remediation")
    provider = _text(remediation["provider"], label="remediation.provider")
    if provider not in _REMEDIATION_PROVIDERS:
        raise _InvalidRuntimeConfig("remediation.provider is not supported")
    model_ref = _text(remediation["model_ref"], label="remediation.model_ref")
    try:
        workspace_grant = WorkspaceGrant.model_validate(
            remediation["workspace_grant"]
        )
        policy = RemediationPolicy.model_validate(remediation["policy"])
    except (TypeError, ValueError, ValidationError):
        raise _InvalidRuntimeConfig("remediation resources are invalid") from None
    if type(workspace_grant) is not WorkspaceGrant or type(policy) is not RemediationPolicy:
        raise _InvalidRuntimeConfig("remediation resources are invalid")
    return AssuranceRemediationRuntimeConfig(
        provider=provider,
        model_ref=model_ref,
        workspace_grant=workspace_grant,
        policy=policy,
    )


def _parse_config(value: object) -> AssuranceRuntimeConfig:
    if type(value) is not dict:
        raise _InvalidRuntimeConfig("configuration has an invalid shape")
    schema_version = value.get("schema_version")
    if schema_version == "v1":
        config = _mapping(value, keys=_CONFIG_V1_KEYS, label="configuration")
        remediation = None
    elif schema_version == "v2":
        config = _mapping(value, keys=_CONFIG_V2_KEYS, label="configuration")
        remediation = _remediation_runtime_config(config["remediation"])
    else:
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
        schema_version=schema_version,
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
        remediation=remediation,
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
        remediation=config.remediation,
    )


def _default_remediation_adapter_factory(
    provider: str, model_ref: str, api_key: str
) -> ModelAdapter:
    """Construct one explicitly configured existing adapter, without routing."""

    if provider in {"qwen", "dashscope"}:
        adapter = DashScopeAdapter(api_key=api_key, model=model_ref)
    elif provider == "gemini":
        adapter = GeminiAdapter(api_key=api_key, model=model_ref)
    elif provider in {"volcengine", "doubao"}:
        adapter = VolcEngineAdapter(api_key=api_key, endpoint_id=model_ref)
    elif provider == "minimax":
        adapter = MiniMaxAdapter(api_key=api_key, model=model_ref)
    elif provider == "deepseek":
        adapter = DeepSeekAdapter(
            api_key=api_key,
            model=model_ref,
            json_mode=True,
        )
    else:
        raise ValueError("remediation provider is not supported")
    if not isinstance(adapter, ModelAdapter):
        raise TypeError("remediation adapter does not satisfy ModelAdapter")
    return adapter


def _adapter_close_handle(adapter: ModelAdapter) -> Callable[[], Any]:
    """Return the only runtime-owned close seam for a remediation adapter."""

    close = getattr(adapter, "aclose", None)
    if callable(close):
        return close
    client = getattr(adapter, "client", None)
    close = getattr(client, "aclose", None)
    if close is None:
        close = getattr(client, "close", None)
    if callable(close):
        return close
    return lambda: None


def _remediation_checks(
    commands: tuple[CommandSpec, ...],
) -> tuple[ValidationCheck, ...]:
    try:
        return tuple(
            ValidationCheck(
                id=command.command_id,
                argv=command.argv,
                visibility="agent",
                timeout_s=command.timeout_seconds,
                output_limit=command.max_output_bytes,
            )
            for command in commands
        )
    except (TypeError, ValueError, ValidationError):
        raise AssuranceRuntimeStartupError() from None


def _revalidate_remediation_root(path: object, configured_root: Path) -> Path:
    """Recheck a server-bound source/workspace path before each preparation."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("remediation source path is invalid")
    try:
        resolved = _real_directory(path, label="remediation source")
    except (_InvalidRuntimeConfig, _PathAccessError):
        raise ValueError("remediation source path is invalid") from None
    if not resolved.is_relative_to(configured_root) or resolved == configured_root:
        raise ValueError("remediation source path is outside the configured workspace")
    return resolved


def _scoped_run_config(
    config: AssuranceRuntimeConfig,
    root: Path,
) -> AssuranceRunConfig:
    return AssuranceRunConfig(
        workspace_root=root,
        allowed_commands=config.allowed_commands,
        orchestration_version=config.orchestration_version,
        redaction_policy_version=config.redaction_policy_version,
        policy_version=config.policy_version,
        rubric_version=config.rubric_version,
        freshness_ttl_seconds=config.freshness_ttl_seconds,
        reviewer_route=config.reviewer,
    )


class _RemediationGitCollector:
    """Keep the scoped collector's facts bound to the controller subject."""

    def __init__(self, collector: object, subject_getter: Callable[[], SubjectDigestInput | None]):
        self._collector = collector
        self._subject_getter = subject_getter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._collector, name)

    def collect(self, *args: Any, **kwargs: Any) -> GitSnapshotResult:
        result = self._collector.collect(*args, **kwargs)
        if type(result) is not GitSnapshotResult:
            raise TypeError("Git collector returned an invalid result")
        subject_input = self._subject_getter()
        if subject_input is None:
            return result
        snapshot = result.snapshot
        if (
            snapshot.repository != subject_input.repository
            or snapshot.base_revision != subject_input.base_revision
            or snapshot.head_revision != subject_input.head_revision
        ):
            raise ValueError("scoped Git facts do not match remediation subject")
        subject_digest = compute_subject_digest(subject_input)
        if snapshot.subject_digest != subject_digest:
            raise ValueError(
                "scoped Git subject digest does not match remediation subject"
            )
        return result


def _build_remediation_service(
    *,
    config: AssuranceRuntimeConfig,
    remediation_api_key: str | None,
    remediation_adapter_factory: RemediationAdapterFactory | None,
    repository: AssuranceWebRepository,
    artifact_store: ArtifactStore,
    context_builder: SafeReviewerContextBuilder,
    reviewer_invoker: FixedOpenAICompatibleReviewerInvoker,
    service: AssuranceRunService,
) -> tuple[
    AssuranceRemediationService | None,
    ModelAdapter | None,
    Callable[[], Any] | None,
]:
    remediation = config.remediation
    if remediation is None or remediation_api_key is None:
        return None, None, None
    factory = remediation_adapter_factory or _default_remediation_adapter_factory
    adapter = factory(
        remediation.provider,
        remediation.model_ref,
        remediation_api_key,
    )
    if inspect.isawaitable(adapter) or not isinstance(adapter, ModelAdapter):
        raise TypeError("remediation adapter factory returned an invalid adapter")

    agent = StructuredRemediationAgent(adapter)
    remediation_config = AssuranceRemediationConfig(
        workspace_grant=remediation.workspace_grant,
        policy=remediation.policy,
    )
    checks = _remediation_checks(config.allowed_commands)

    async def _compose_preparation_controller(
        request: RemediationRequest,
        *,
        context: RemediationContext,
    ) -> Any:
        if type(request) is not RemediationRequest:
            raise TypeError("remediation request is invalid")
        if type(context) is not RemediationContext:
            raise TypeError("remediation context is invalid")
        baseline = context.baseline_bundle
        selected_finding = context.selected_finding
        source_binding = context.source_binding
        if (
            type(baseline) is not AssuranceRunBundle
            or type(selected_finding) is not Finding
            or type(source_binding) is not FreshnessSourceBinding
            or baseline.freshness_source_binding != source_binding
        ):
            raise ValueError("remediation context facts are invalid")
        source_root = _revalidate_remediation_root(
            source_binding.repository_path,
            config.workspace_root,
        )
        if (
            request.old_case_id != baseline.case.case_id
            or request.old_subject_digest != baseline.subject.subject_digest
            or request.human_selected_finding_id != selected_finding.finding_id
            or selected_finding.subject_digest != baseline.subject.subject_digest
        ):
            raise ValueError("remediation request is not bound to its context")

        requested_subject_input: SubjectDigestInput | None = None

        def subject_builder(
            _patch_digest: str, *, workspace: IsolatedWorkspace
        ) -> tuple[SubjectDigestInput, str]:
            nonlocal requested_subject_input
            if type(workspace) is not IsolatedWorkspace:
                raise ValueError("remediation subject builder requires its workspace")
            scoped_root = _revalidate_remediation_root(
                workspace.root, config.workspace_root
            )
            collector = service._git_collector
            build_subject_input = getattr(collector, "build_subject_input", None)
            if not callable(build_subject_input):
                raise ValueError("remediation Git collector cannot rebuild subject")
            with tempfile.TemporaryDirectory(
                prefix="codemesh-remediation-subject-"
            ) as scratch_root:
                scratch_store = ArtifactStore(Path(scratch_root))
                task_digest = service._intake_collector.probe_task_digest(
                    scoped_root, task_path=source_binding.task_path
                )
                git_result = collector.collect(
                    scoped_root,
                    repository_identity=source_binding.repository_identity,
                    base_ref=source_binding.requested_base_ref,
                    task_digest=task_digest,
                    policy_version=source_binding.policy_version,
                    rubric_version=source_binding.rubric_version,
                    artifact_store=scratch_store,
                    attachment_digests=source_binding.attachment_digests,
                )
                if type(git_result) is not GitSnapshotResult:
                    raise TypeError("Git collector returned an invalid result")
                subject_input = build_subject_input(
                    git_result.snapshot,
                    task_digest=task_digest,
                    policy_version=source_binding.policy_version,
                    rubric_version=source_binding.rubric_version,
                    attachment_digests=source_binding.attachment_digests,
                )
            requested_subject_input = subject_input
            return subject_input, compute_subject_digest(subject_input)

        def reviewer_service_factory(workspace_root: Path) -> AssuranceRunService:
            scoped_root = _revalidate_remediation_root(
                workspace_root, config.workspace_root
            )
            scoped_config = _scoped_run_config(config, scoped_root)
            return AssuranceRunService(
                artifact_store=artifact_store,
                reviewer_invoker=reviewer_invoker,
                committer=repository,
                context_builder=context_builder,
                config=scoped_config,
                git_collector=_RemediationGitCollector(
                    service._git_collector,
                    lambda: requested_subject_input,
                ),
                intake_collector=service._intake_collector,
            )

        reviewer = AssuranceRemediationReviewer(
            baseline_bundle=baseline,
            service_factory=reviewer_service_factory,
        )

        controller = RemediationController(
            request=request,
            selected_finding=selected_finding,
            seed_root=source_root,
            validation_executor=lambda workspace: ValidationExecutor(
                workspace=workspace,
                checks=checks,
            ),
            subject_builder=subject_builder,
            reviewer_rerunner=reviewer,
            workspace_parent=config.workspace_root,
        )
        return controller

    async def prepare_callback(
        request: RemediationRequest,
        *,
        context: RemediationContext,
    ) -> Any:
        try:
            controller = await _compose_preparation_controller(
                request,
                context=context,
            )
        except AssuranceRemediationPreparationError:
            raise
        except (AssuranceRemediationError, AssuranceWebError, StoreConflictError):
            raise
        except Exception as exc:
            raise AssuranceRemediationPreparationError(
                stage=RemediationPreparationStage.SOURCE_RUNTIME.value,
                reason_code=_preparation_reason_code(exc),
            ) from exc

        try:
            return await controller.prepare(agent)
        except AssuranceRemediationPreparationError:
            raise
        except (AssuranceRemediationError, AssuranceWebError, StoreConflictError):
            raise
        except Exception as exc:
            raise AssuranceRemediationPreparationError(
                stage=RemediationPreparationStage.CONTROLLER_PREPARATION.value,
                reason_code=_preparation_reason_code(exc),
            ) from exc

    remediation_service = AssuranceRemediationService(
        repository,
        prepare_callback=prepare_callback,
        config=remediation_config,
    )
    return remediation_service, adapter, _adapter_close_handle(adapter)


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


def _environment_values(
    environ: Mapping[str, str] | None,
) -> tuple[object, object, str | None] | None:
    source = os.environ if environ is None else environ
    if not isinstance(source, Mapping):
        return None
    try:
        config_ref = source.get(_CONFIG_ENV)
        reviewer_api_key = source.get(_API_KEY_ENV)
        remediation_api_key = source.get(_REMEDIATION_API_KEY_ENV)
    except Exception:
        return None
    if type(config_ref) is not str or not config_ref.strip():
        return None
    if type(reviewer_api_key) is not str or not reviewer_api_key.strip():
        return None
    if type(remediation_api_key) is not str or not remediation_api_key.strip():
        remediation_api_key = None
    return config_ref, reviewer_api_key, remediation_api_key


def _build_runtime(
    config: AssuranceRuntimeConfig,
    reviewer_api_key: str,
    reviewer_transport: httpx.AsyncBaseTransport | None,
    remediation_api_key: str | None,
    remediation_adapter_factory: RemediationAdapterFactory | None,
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
            api_key=SecretStr(reviewer_api_key),
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
        (
            remediation_service,
            remediation_adapter,
            remediation_adapter_close,
        ) = _build_remediation_service(
            config=config,
            remediation_api_key=remediation_api_key,
            remediation_adapter_factory=remediation_adapter_factory,
            repository=repository,
            artifact_store=artifact_store,
            context_builder=context_builder,
            reviewer_invoker=reviewer_invoker,
            service=service,
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
        remediation_service=remediation_service,
        remediation_adapter=remediation_adapter,
        remediation_adapter_close=remediation_adapter_close,
    )


def load_assurance_runtime_from_environment(
    environ: Mapping[str, str] | None = None,
    reviewer_transport: httpx.AsyncBaseTransport | None = None,
    remediation_adapter_factory: RemediationAdapterFactory | None = None,
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
    config_ref, reviewer_api_key, remediation_api_key = values
    config = _read_config(config_ref)
    if config is None:
        return None
    try:
        config = _validate_paths(config)
    except _InvalidRuntimeConfig:
        return None
    except _PathAccessError:
        raise AssuranceRuntimeStartupError() from None
    return _build_runtime(
        config,
        reviewer_api_key,
        reviewer_transport,
        remediation_api_key,
        remediation_adapter_factory,
    )


__all__ = [
    "AssuranceRuntime",
    "AssuranceRuntimeConfig",
    "AssuranceRuntimeStartupError",
    "load_assurance_runtime_from_environment",
]
