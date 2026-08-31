"""保障域主题摘要：规范化、规范载荷和不可变摘要。"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_serializer,
    model_validator,
)

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_JSON_KWARGS = {
    "sort_keys": True,
    "separators": (",", ":"),
    "ensure_ascii": False,
    "allow_nan": False,
}


def normalize_repository_identity(value: str) -> str:
    """规范化仓库身份：NFC、去首尾空白、反斜杠转正斜杠、去尾部斜杠。"""
    if not isinstance(value, str):
        raise TypeError("repository identity must be a str")
    normalized = unicodedata.normalize("NFC", value).strip().replace("\\", "/")
    normalized = normalized.rstrip("/")
    if not normalized:
        raise ValueError("repository identity must not be empty")
    return normalized


def normalize_repo_path(value: str) -> str:
    """规范化相对仓库路径，拒绝绝对、UNC、盘符、NUL 与 .. 段。"""
    if not isinstance(value, str):
        raise TypeError("repo path must be a str")
    normalized = unicodedata.normalize("NFC", value).replace("\\", "/")
    if "\x00" in normalized:
        raise ValueError("repo path must not contain NUL")
    if not normalized:
        raise ValueError("repo path must not be empty")
    if normalized.startswith("//"):
        raise ValueError("UNC paths are not allowed")
    if normalized.startswith("/"):
        raise ValueError("POSIX absolute paths are not allowed")
    if re.match(r"^[A-Za-z]:/", normalized) is not None:
        raise ValueError("Windows drive paths are not allowed")
    segments = []
    for segment in normalized.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise ValueError("repo path must not contain '..' segments")
        segments.append(segment)
    if not segments:
        raise ValueError("repo path must contain at least one segment")
    return "/".join(segments)


def normalize_line_endings(value: str) -> str:
    """仅将 CRLF 与孤立 CR 转换为 LF，其余内容原样保留。"""
    if not isinstance(value, str):
        raise TypeError("line-ending text must be a str")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def compute_normalized_diff_digest(
    entries: Sequence[tuple[str, str]],
) -> str:
    """对规范化后的 (path, patch) 列表计算稳定 SHA-256 摘要。"""
    if not isinstance(entries, Sequence) or isinstance(
        entries, (str, bytes, bytearray)
    ):
        raise TypeError("entries must be a sequence of (path, patch) pairs")
    if not entries:
        raise ValueError("at least one diff entry is required")
    files = []
    seen_paths = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError(f"entry {index} must be a (path, patch) pair")
        raw_path, raw_patch = entry
        if not isinstance(raw_path, str):
            raise TypeError(f"entry {index} path must be a str")
        if not isinstance(raw_patch, str):
            raise TypeError(f"entry {index} patch must be a str")
        path = normalize_repo_path(raw_path)
        patch = normalize_line_endings(raw_patch)
        if path in seen_paths:
            raise ValueError(f"duplicate normalized path: {path}")
        seen_paths.add(path)
        files.append({"path": path, "patch": patch})
    files.sort(key=lambda item: item["path"])
    payload = {"schema_version": "v1", "files": files}
    encoded = json.dumps(payload, **_JSON_KWARGS).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class SubjectDigestInput(BaseModel):
    """主题摘要输入的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1", "v2"] = "v1"
    repository: str
    base_revision: str
    head_revision: str
    normalized_diff_digest: str
    task_digest: str
    policy_version: str
    rubric_version: str
    attachment_digests: tuple[str, ...] = ()
    acceptance_scope_digest: str | None = None

    @field_validator("repository", mode="before")
    @classmethod
    def _normalize_repository(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("repository must be a str")
        return normalize_repository_identity(value)

    @field_validator(
        "base_revision",
        "head_revision",
        "policy_version",
        "rubric_version",
        mode="before",
    )
    @classmethod
    def _validate_identity_text(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("identity field must be a str")
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("normalized_diff_digest", "task_digest", mode="before")
    @classmethod
    def _validate_sha256_digest(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("digest must be a str")
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator("attachment_digests", mode="before")
    @classmethod
    def _validate_attachment_digests(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (tuple, list)):
            raise ValueError("attachment_digests must be a tuple or list")
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("attachment digest must be a str")
            if _SHA256_DIGEST_RE.fullmatch(item) is None:
                raise ValueError(
                    "must be a lowercase sha256:<64 hex> digest"
                )
            if item in seen:
                raise ValueError("attachment digests must be unique")
            seen.add(item)
            result.append(item)
        return tuple(sorted(result))

    @field_validator("acceptance_scope_digest", mode="before")
    @classmethod
    def _validate_acceptance_scope_digest(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("acceptance_scope_digest must be a str")
        if _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @model_validator(mode="after")
    def _require_scope_for_v2(self) -> "SubjectDigestInput":
        if self.schema_version == "v2" and self.acceptance_scope_digest is None:
            raise ValueError("v2 subject identity requires acceptance_scope_digest")
        if self.schema_version == "v1" and self.acceptance_scope_digest is not None:
            raise ValueError("v1 subject identity must not carry acceptance scope")
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(
        self, handler
    ) -> dict[str, object]:
        payload = handler(self)
        if self.schema_version == "v1":
            payload.pop("acceptance_scope_digest", None)
        return payload


class AcceptanceScopeDigestInput(BaseModel):
    """The canonical, repository-relative declarations in an acceptance scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    task_path: str
    policy_paths: tuple[str, ...] = ()
    adr_paths: tuple[str, ...] = ()
    runbook_paths: tuple[str, ...] = ()

    @field_validator("task_path", mode="before")
    @classmethod
    def _task_path(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("task_path must be a string")
        normalized = normalize_repo_path(value)
        if normalized != value or not value.endswith(".md"):
            raise ValueError("task_path must be a canonical .md path")
        return value

    @field_validator("policy_paths", "adr_paths", "runbook_paths", mode="before")
    @classmethod
    def _declared_paths(cls, value: object, info) -> tuple[str, ...]:
        if type(value) not in (tuple, list):
            raise ValueError(f"{info.field_name} must be a tuple or list")
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            if type(item) is not str:
                raise ValueError(f"{info.field_name} must contain strings")
            normalized = normalize_repo_path(item)
            if normalized != item or not item.endswith(".md"):
                raise ValueError(
                    f"{info.field_name} must contain canonical .md paths"
                )
            if item in seen:
                raise ValueError(f"{info.field_name} must be unique")
            seen.add(item)
            result.append(item)
        return tuple(result)


def canonical_acceptance_scope_payload(
    value: AcceptanceScopeDigestInput,
) -> bytes:
    """Return the four-field canonical payload for one acceptance scope."""

    if not isinstance(value, AcceptanceScopeDigestInput):
        raise TypeError("value must be an AcceptanceScopeDigestInput")
    payload = {
        "task_path": value.task_path,
        "policy_paths": list(value.policy_paths),
        "adr_paths": list(value.adr_paths),
        "runbook_paths": list(value.runbook_paths),
    }
    return json.dumps(payload, **_JSON_KWARGS).encode("utf-8")


def compute_acceptance_scope_digest(
    value: AcceptanceScopeDigestInput,
) -> str:
    """Compute the stable SHA-256 digest of an acceptance scope."""

    return "sha256:" + hashlib.sha256(
        canonical_acceptance_scope_payload(value)
    ).hexdigest()


def canonical_subject_payload(value: SubjectDigestInput) -> bytes:
    """返回仅含 SubjectDigestInput 字段的稳定 UTF-8 规范载荷。"""
    if not isinstance(value, SubjectDigestInput):
        raise TypeError("value must be a SubjectDigestInput")
    return json.dumps(
        value.model_dump(mode="json"),
        **_JSON_KWARGS,
    ).encode("utf-8")


def compute_subject_digest(value: SubjectDigestInput) -> str:
    """计算主题的稳定 SHA-256 摘要。"""
    return "sha256:" + hashlib.sha256(canonical_subject_payload(value)).hexdigest()


def changed_subject_fields(
    before: SubjectDigestInput,
    after: SubjectDigestInput,
) -> tuple[str, ...]:
    """按声明顺序返回 before 与 after 中变化的字段名。"""
    if not isinstance(before, SubjectDigestInput) or not isinstance(
        after, SubjectDigestInput
    ):
        raise TypeError("before and after must be SubjectDigestInput instances")
    changed = []
    for name in SubjectDigestInput.model_fields:
        if getattr(before, name) != getattr(after, name):
            changed.append(name)
    return tuple(changed)
