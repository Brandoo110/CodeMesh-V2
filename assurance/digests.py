"""保障域主题摘要：规范化、规范载荷和不可变摘要。"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

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

    schema_version: Literal["v1"] = "v1"
    repository: str
    base_revision: str
    head_revision: str
    normalized_diff_digest: str
    task_digest: str
    policy_version: str
    rubric_version: str
    attachment_digests: tuple[str, ...] = ()

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
