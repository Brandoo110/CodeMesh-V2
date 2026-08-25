"""P3-01B 风险分类：纯确定性规则引擎与防伪绑定。

本模块只做输入模型上的纯分类推导与摘要绑定：不读取路径/文件/网络/API/
仓库/Git，不调用 Collector/Policy Gate/Reviewer/路由/编排，不执行子进程、
shell、eval/exec，不持有可变全局状态，也不依赖时间/随机/环境。
"""

import hashlib
import json
import re
from types import MappingProxyType
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .intake import IntakeSnapshot
from .manifest import EvidenceManifest
from .snapshot import GitChange, GitSnapshot


_RISK_ID_RE = re.compile(r"risk_[0-9a-f]{32}\Z")
_SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RULES_VERSION = "risk.v0"
_REVIEWER_ORDER = ("intent", "architecture", "operability")

_REASON_SIGNALS = (
    ("AUTHORIZATION_CHANGE", "authorization"),
    ("MIGRATION_SCHEMA_CHANGE", "migration_schema"),
    ("PUBLIC_API_CHANGE", "public_api"),
    ("DEPENDENCY_LOCKFILE_CHANGE", "dependency_lockfile"),
    ("CI_IAC_CHANGE", "ci_iac"),
    ("EXTERNAL_SIDE_EFFECTS_PRESENT", "side_effects_present"),
    ("EXTERNAL_SIDE_EFFECTS_UNKNOWN", "side_effects_unknown"),
    ("LARGE_CHANGE_FILE_COUNT", "files_medium"),
    ("LARGE_CHANGE_LINE_COUNT", "lines_medium"),
    ("CHANGE_LINE_COUNT_UNKNOWN", "lines_unknown"),
    ("CROSS_MODULE_CHANGE", "modules_medium"),
    ("POLICY_ADR_CHANGE", "policy_adr_changed"),
    ("EVIDENCE_GAPS", "evidence_gaps"),
    ("INTAKE_INCOMPLETE", "intake_incomplete"),
    ("PROVIDER_BOUNDARY_CROSSING", "provider_crossing"),
    ("PROVIDER_BOUNDARY_UNKNOWN", "provider_unknown"),
)
_REASON_ORDER = tuple(reason for reason, _ in _REASON_SIGNALS)

_AUTH_SEGMENTS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "iam",
        "rbac",
        "permission",
        "permissions",
        "acl",
        "oauth",
        "oauth2",
    }
)
_MIGRATION_SEGMENTS = frozenset(
    {"migration", "migrations", "alembic", "schema", "schemas"}
)
_MIGRATION_BASENAME_SUFFIXES = (".sql",)
_PUBLIC_API_SEGMENTS = frozenset({"api", "routes", "openapi"})
_PUBLIC_API_BASENAMES = frozenset(
    {"openapi.json", "openapi.yaml", "openapi.yml"}
)
_DEPENDENCY_BASENAMES = frozenset(
    {
        "pyproject.toml",
        "poetry.lock",
        "pdm.lock",
        "uv.lock",
        "pipfile",
        "pipfile.lock",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "composer.json",
        "composer.lock",
        "gemfile",
        "gemfile.lock",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "gradle.lockfile",
        "packages.lock.json",
    }
)
_REQUIREMENTS_PREFIX = "requirements"
_REQUIREMENTS_SUFFIX = ".txt"
_CI_IAC_PREFIX_SEGMENTS = (".github", "workflows")
_CI_IAC_BASENAMES = frozenset(
    {
        "dockerfile",
        "compose.yml",
        "compose.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
    }
)
_CI_IAC_SEGMENTS = frozenset(
    {"terraform", "k8s", "kubernetes", "helm", "infra", "infrastructure"}
)

_BASE_COLLECTORS = (
    "git_snapshot",
    "task_policy_adr",
    "deterministic_commands",
    "evidence_manifest",
)
_ADDITIONAL_COLLECTOR_ORDER = (
    "authz_validation",
    "migration_validation",
    "api_contract",
    "dependency_audit",
    "ci_iac_validation",
    "side_effect_validation",
    "provider_boundary_attestation",
)
_COLLECTOR_REASONS = MappingProxyType(
    {
        "authz_validation": frozenset({"AUTHORIZATION_CHANGE"}),
        "migration_validation": frozenset({"MIGRATION_SCHEMA_CHANGE"}),
        "api_contract": frozenset({"PUBLIC_API_CHANGE"}),
        "dependency_audit": frozenset({"DEPENDENCY_LOCKFILE_CHANGE"}),
        "ci_iac_validation": frozenset({"CI_IAC_CHANGE"}),
        "side_effect_validation": frozenset(
            {
                "EXTERNAL_SIDE_EFFECTS_PRESENT",
                "EXTERNAL_SIDE_EFFECTS_UNKNOWN",
            }
        ),
        "provider_boundary_attestation": frozenset(
            {
                "PROVIDER_BOUNDARY_CROSSING",
                "PROVIDER_BOUNDARY_UNKNOWN",
            }
        ),
    }
)

_ARCHITECTURE_REVIEW_REASONS = frozenset(
    {
        "AUTHORIZATION_CHANGE",
        "MIGRATION_SCHEMA_CHANGE",
        "PUBLIC_API_CHANGE",
        "DEPENDENCY_LOCKFILE_CHANGE",
        "CROSS_MODULE_CHANGE",
        "POLICY_ADR_CHANGE",
        "PROVIDER_BOUNDARY_CROSSING",
    }
)
_OPERABILITY_REVIEW_REASONS = frozenset(
    {
        "MIGRATION_SCHEMA_CHANGE",
        "CI_IAC_CHANGE",
        "EXTERNAL_SIDE_EFFECTS_PRESENT",
        "EXTERNAL_SIDE_EFFECTS_UNKNOWN",
        "LARGE_CHANGE_FILE_COUNT",
        "LARGE_CHANGE_LINE_COUNT",
        "CHANGE_LINE_COUNT_UNKNOWN",
        "EVIDENCE_GAPS",
        "INTAKE_INCOMPLETE",
        "PROVIDER_BOUNDARY_UNKNOWN",
        "PROVIDER_BOUNDARY_CROSSING",
    }
)
_HIGH_SIGNALS = frozenset(
    {
        "authorization",
        "migration_schema",
        "public_api",
        "dependency_lockfile",
        "ci_iac",
        "side_effects_present",
        "policy_adr_changed",
        "provider_crossing",
        "evidence_gaps",
        "intake_incomplete",
        "files_high",
        "lines_high",
        "modules_high",
    }
)
_MEDIUM_SIGNALS = frozenset(
    {
        "lines_unknown",
        "side_effects_unknown",
        "provider_unknown",
        "files_medium",
        "lines_medium",
        "modules_medium",
    }
)

_THRESHOLDS = MappingProxyType(
    {
        "changed_files_medium": 5,
        "changed_files_high": 20,
        "changed_lines_medium": 200,
        "changed_lines_high": 1000,
        "cross_module_medium": 2,
        "cross_module_high": 3,
    }
)
_OPERATORS = MappingProxyType(
    {"files": ">", "lines": ">", "modules": ">="}
)

_RULES_TABLE = MappingProxyType(
    {
        "rules_version": _RULES_VERSION,
        "reason_codes": _REASON_ORDER,
        "path_rules": MappingProxyType(
            {
                "authorization_segments": _AUTH_SEGMENTS,
                "migration_segments": _MIGRATION_SEGMENTS,
                "migration_basename_suffixes": _MIGRATION_BASENAME_SUFFIXES,
                "public_api_segments": _PUBLIC_API_SEGMENTS,
                "public_api_basenames": _PUBLIC_API_BASENAMES,
                "dependency_basenames": _DEPENDENCY_BASENAMES,
                "requirements_basename_prefix": _REQUIREMENTS_PREFIX,
                "requirements_basename_suffix": _REQUIREMENTS_SUFFIX,
                "ci_iac_prefix_segments": _CI_IAC_PREFIX_SEGMENTS,
                "ci_iac_basenames": _CI_IAC_BASENAMES,
                "ci_iac_segments": _CI_IAC_SEGMENTS,
            }
        ),
        "thresholds": _THRESHOLDS,
        "operators": _OPERATORS,
        "risk_level_rules": MappingProxyType(
            {
                "high_signals": _HIGH_SIGNALS,
                "medium_signals": _MEDIUM_SIGNALS,
            }
        ),
        "reviewer_rules": MappingProxyType(
            {
                "base_reviewers": ("intent",),
                "architecture_reasons": _ARCHITECTURE_REVIEW_REASONS,
                "operability_reasons": _OPERABILITY_REVIEW_REASONS,
                "high_forces_all_reviewers": True,
            }
        ),
        "collector_rules": MappingProxyType(
            {
                "base_collectors": _BASE_COLLECTORS,
                "additional_order": _ADDITIONAL_COLLECTOR_ORDER,
                "reason_mappings": _COLLECTOR_REASONS,
            }
        ),
    }
)


def _jsonable(value):
    if isinstance(value, MappingProxyType):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(_jsonable(item) for item in value)
    return value


def _canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


_RULES_DIGEST = _sha256_digest(
    _canonical_json_bytes(_jsonable(_RULES_TABLE))
)


def _classification_id_from_data(data: dict) -> str:
    body = {
        key: value for key, value in data.items() if key != "classification_id"
    }
    envelope = {
        "subject_digest": data["subject_digest"],
        "rules_digest": data["rules_digest"],
        "facts_digest": data["facts_digest"],
        "classification_body": body,
    }
    return "risk_" + hashlib.sha256(
        _canonical_json_bytes(envelope)
    ).hexdigest()[:32]


def _facts_digest(value: "RiskClassificationInput") -> str:
    return _sha256_digest(
        _canonical_json_bytes(value.model_dump(mode="json"))
    )


def _path_signals(changes):
    exact_paths = set()
    module_names = set()
    auth = False
    migration = False
    public_api = False
    dependency = False
    ci_iac = False
    for change in changes:
        lower = change.path.lower()
        segments = lower.split("/")
        basename = segments[-1]
        exact_paths.add(change.path)
        if any(segment in _AUTH_SEGMENTS for segment in segments):
            auth = True
        if (
            any(segment in _MIGRATION_SEGMENTS for segment in segments)
            or basename.endswith(_MIGRATION_BASENAME_SUFFIXES)
        ):
            migration = True
        if (
            any(segment in _PUBLIC_API_SEGMENTS for segment in segments)
            or basename in _PUBLIC_API_BASENAMES
        ):
            public_api = True
        if (
            basename in _DEPENDENCY_BASENAMES
            or (
                basename.startswith(_REQUIREMENTS_PREFIX)
                and basename.endswith(_REQUIREMENTS_SUFFIX)
            )
        ):
            dependency = True
        if (
            (
                len(segments) >= 2
                and segments[0] == _CI_IAC_PREFIX_SEGMENTS[0]
                and segments[1] == _CI_IAC_PREFIX_SEGMENTS[1]
            )
            or basename in _CI_IAC_BASENAMES
            or any(segment in _CI_IAC_SEGMENTS for segment in segments)
        ):
            ci_iac = True
        if "/" in change.path:
            module_names.add(segments[0])
    return (
        exact_paths,
        auth,
        migration,
        public_api,
        dependency,
        ci_iac,
        module_names,
    )


def _derive_classification(
    value: "RiskClassificationInput",
) -> "RiskClassification":
    facts_digest = _facts_digest(value)
    (
        exact_paths,
        auth,
        migration,
        public_api,
        dependency,
        ci_iac,
        module_names,
    ) = _path_signals(value.snapshot.changes)

    files_medium = (
        value.snapshot.changed_files_total
        > _THRESHOLDS["changed_files_medium"]
    )
    files_high = (
        value.snapshot.changed_files_total
        > _THRESHOLDS["changed_files_high"]
    )
    lines_total = value.declarations.changed_lines_total
    lines_medium = (
        lines_total is not None
        and lines_total > _THRESHOLDS["changed_lines_medium"]
    )
    lines_high = (
        lines_total is not None
        and lines_total > _THRESHOLDS["changed_lines_high"]
    )
    lines_unknown = lines_total is None
    modules_medium = (
        len(module_names) >= _THRESHOLDS["cross_module_medium"]
    )
    modules_high = len(module_names) >= _THRESHOLDS["cross_module_high"]
    side_effects_present = (
        value.declarations.external_side_effects == "present_declared"
    )
    side_effects_unknown = (
        value.declarations.external_side_effects == "unknown"
    )
    provider_crossing = (
        value.declarations.provider_boundary == "crosses_declared_boundary"
    )
    provider_unknown = value.declarations.provider_boundary == "unknown"
    policy_adr_changed = any(
        document.kind in ("policy", "adr")
        and document.path in exact_paths
        for document in value.intake.documents
    )
    evidence_gaps = value.manifest.completeness_status == "has_gaps"
    intake_incomplete = value.intake.complete is False

    signals = {
        "authorization": auth,
        "migration_schema": migration,
        "public_api": public_api,
        "dependency_lockfile": dependency,
        "ci_iac": ci_iac,
        "side_effects_present": side_effects_present,
        "side_effects_unknown": side_effects_unknown,
        "files_medium": files_medium,
        "files_high": files_high,
        "lines_medium": lines_medium,
        "lines_high": lines_high,
        "lines_unknown": lines_unknown,
        "modules_medium": modules_medium,
        "modules_high": modules_high,
        "policy_adr_changed": policy_adr_changed,
        "evidence_gaps": evidence_gaps,
        "intake_incomplete": intake_incomplete,
        "provider_crossing": provider_crossing,
        "provider_unknown": provider_unknown,
    }
    reasons = tuple(
        reason
        for reason, signal_name in _REASON_SIGNALS
        if signals[signal_name]
    )
    reason_set = frozenset(reasons)
    if any(signals[name] for name in _HIGH_SIGNALS):
        risk_level = "high"
    elif any(signals[name] for name in _MEDIUM_SIGNALS):
        risk_level = "medium"
    else:
        risk_level = "low"

    if risk_level == "high":
        required_reviewers = _REVIEWER_ORDER
    else:
        required_reviewers = ("intent",)
        if reason_set & _ARCHITECTURE_REVIEW_REASONS:
            required_reviewers += ("architecture",)
        if reason_set & _OPERABILITY_REVIEW_REASONS:
            required_reviewers += ("operability",)

    required_collectors = list(_BASE_COLLECTORS)
    for collector_name in _ADDITIONAL_COLLECTOR_ORDER:
        if reason_set & _COLLECTOR_REASONS[collector_name]:
            required_collectors.append(collector_name)
    required_collectors = tuple(required_collectors)

    classification_data = {
        "schema_version": "v1",
        "classification_id": "",
        "subject_digest": value.snapshot.subject_digest,
        "rules_version": _RULES_VERSION,
        "rules_digest": _RULES_DIGEST,
        "facts_digest": facts_digest,
        "risk_level": risk_level,
        "reason_codes": reasons,
        "required_collectors": required_collectors,
        "required_reviewers": required_reviewers,
        "required_human_role": (
            "change_owner" if risk_level == "high" else None
        ),
    }
    classification_data["classification_id"] = _classification_id_from_data(
        classification_data
    )
    return RiskClassification(**classification_data)


class RiskDeclarations(BaseModel):
    """风险分类输入声明：冻结、禁止额外字段、v1 合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    changed_lines_total: StrictInt | None = Field(ge=0)
    external_side_effects: Literal["none_declared", "present_declared", "unknown"]
    provider_boundary: Literal[
        "within_declared_boundary", "crosses_declared_boundary", "unknown"
    ]


class RiskClassificationInput(BaseModel):
    """风险分类输入：三个嵌套快照必须是各自模型的精确实例。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    snapshot: GitSnapshot
    intake: IntakeSnapshot
    manifest: EvidenceManifest
    declarations: RiskDeclarations

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(cls, data: object) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "RiskClassificationInput must validate from a mapping"
            )
        expected = {
            "snapshot": GitSnapshot,
            "intake": IntakeSnapshot,
            "manifest": EvidenceManifest,
            "declarations": RiskDeclarations,
        }
        for field_name, model_type in expected.items():
            if type(data.get(field_name)) is not model_type:
                raise ValueError(
                    f"{field_name} must be an exact {model_type.__name__} instance"
                )
        return data

    @model_validator(mode="after")
    def _require_consistent_subject(self) -> "RiskClassificationInput":
        subject_digest = self.snapshot.subject_digest
        if (
            subject_digest != self.intake.subject_digest
            or subject_digest != self.manifest.subject_digest
        ):
            raise ValueError(
                "snapshot, intake, and manifest subject_digest must match"
            )
        return self


class RiskClassification(BaseModel):
    """风险分类结果合同：语法、结构、规则摘要与派生 ID 绑定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    classification_id: str
    subject_digest: str
    rules_version: Literal["risk.v0"] = "risk.v0"
    rules_digest: str
    facts_digest: str
    risk_level: Literal["low", "medium", "high"]
    reason_codes: tuple[StrictStr, ...]
    required_collectors: tuple[StrictStr, ...]
    required_reviewers: tuple[
        Literal["intent", "architecture", "operability"], ...
    ]
    required_human_role: Literal["change_owner"] | None

    @field_validator("classification_id", mode="before")
    @classmethod
    def _validate_classification_id(cls, value: object) -> str:
        if (
            type(value) is not str
            or _RISK_ID_RE.fullmatch(value) is None
        ):
            raise ValueError(
                "classification_id must be risk_<32 lowercase hex>"
            )
        return value

    @field_validator(
        "subject_digest", "rules_digest", "facts_digest", mode="before"
    )
    @classmethod
    def _validate_sha256_digests(cls, value: object) -> str:
        if (
            type(value) is not str
            or _SHA256_DIGEST_RE.fullmatch(value) is None
        ):
            raise ValueError(
                "must be a lowercase sha256:<64 hex> digest"
            )
        return value

    @field_validator(
        "reason_codes",
        "required_collectors",
        "required_reviewers",
        mode="before",
    )
    @classmethod
    def _exact_tuple_at_raw_validation(
        cls, value: object, info: ValidationInfo
    ) -> object:
        if type(value) is tuple:
            return value
        if info.mode == "json" and type(value) is list:
            return value
        raise ValueError("must be an exact tuple at raw validation")

    @field_validator(
        "reason_codes", "required_collectors", "required_reviewers"
    )
    @classmethod
    def _validate_string_tuple_items(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        seen: set[str] = set()
        for item in value:
            if type(item) is not str:
                raise ValueError("items must be exact strings")
            if not item.strip():
                raise ValueError("items must not be blank or whitespace-only")
            if item in seen:
                raise ValueError("items must be unique")
            seen.add(item)
        return value

    @field_validator("required_reviewers")
    @classmethod
    def _validate_canonical_reviewer_order(
        cls,
        value: tuple[
            Literal["intent", "architecture", "operability"], ...
        ],
    ) -> tuple[Literal["intent", "architecture", "operability"], ...]:
        positions = {
            name: index for index, name in enumerate(_REVIEWER_ORDER)
        }
        previous = -1
        for item in value:
            current = positions[item]
            if current <= previous:
                raise ValueError(
                    "required_reviewers must follow "
                    "intent, architecture, operability order"
                )
            previous = current
        return value

    @model_validator(mode="after")
    def _validate_human_role_cross_field(
        self,
    ) -> "RiskClassification":
        if self.risk_level == "high":
            if self.required_human_role != "change_owner":
                raise ValueError(
                    "high risk requires required_human_role=change_owner"
                )
        elif self.required_human_role is not None:
            raise ValueError(
                "low/medium risk requires required_human_role=None"
            )
        return self

    @model_validator(mode="after")
    def _validate_rules_digest_and_classification_id(
        self,
    ) -> "RiskClassification":
        if self.rules_digest != _RULES_DIGEST:
            raise ValueError(
                "rules_digest must equal the module rules digest"
            )
        expected_id = _classification_id_from_data(
            self.model_dump(mode="json")
        )
        if self.classification_id != expected_id:
            raise ValueError(
                "classification_id must be derived from its other fields"
            )
        return self


class RiskClassificationResult(BaseModel):
    """风险分类完整绑定：输入与同一纯派生分类必须一致。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    input: RiskClassificationInput
    classification: RiskClassification

    @model_validator(mode="before")
    @classmethod
    def _require_exact_nested_models(cls, data: object) -> object:
        if not isinstance(data, dict):
            raise ValueError(
                "RiskClassificationResult must validate from a mapping"
            )
        expected = {
            "input": RiskClassificationInput,
            "classification": RiskClassification,
        }
        for field_name, model_type in expected.items():
            if type(data.get(field_name)) is not model_type:
                raise ValueError(
                    f"{field_name} must be an exact {model_type.__name__} instance"
                )
        return data

    @model_validator(mode="after")
    def _require_derived_classification(
        self,
    ) -> "RiskClassificationResult":
        derived = _derive_classification(self.input)
        if self.classification != derived:
            raise ValueError(
                "classification must equal the derived classification"
            )
        return self


class RiskClassifier:
    """纯确定性风险分类器：无状态、无配置、无 I/O。"""

    @staticmethod
    def classify(value: RiskClassificationInput) -> RiskClassificationResult:
        if type(value) is not RiskClassificationInput:
            raise TypeError("value must be an exact RiskClassificationInput")
        return RiskClassificationResult(
            schema_version="v1",
            input=value,
            classification=_derive_classification(value),
        )
