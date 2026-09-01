"""任务规范、策略、ADR 与运行手册的只读摄入快照收集器（V2-P2-02）。"""

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .contracts import Evidence
from .digests import normalize_repo_path


_MAX_DECLARED_PATHS = 64
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_MAX_FRONTMATTER_BYTES = 16 * 1024
_MAX_FRONTMATTER_ITEMS = 64

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CHECKBOX_RE = re.compile(r"^-\s*\[(?: |x|X)\](?P<rest>.*)$")
_MULTILINE_SCALARS = frozenset({"|", ">", "|-", ">-", "|+", ">+"})

_KIND_ORDER = {"task_spec": 0, "policy": 1, "adr": 2, "runbook": 3}

_NOTICE_SEMANTICS = {
    "task_spec_not_declared": ("missing_evidence", False),
    "task_spec_not_found": ("missing_evidence", True),
    "task_title_missing": ("missing_evidence", True),
    "task_owner_missing": ("missing_evidence", True),
    "acceptance_criteria_missing": ("missing_evidence", True),
    "policy_not_declared": ("missing_evidence", False),
    "policy_not_found": ("missing_evidence", True),
    "adr_not_declared": ("unknown", False),
    "adr_not_found": ("unknown", True),
    "runbook_not_found": ("missing_evidence", True),
}

_NOT_FOUND_RULES = {
    "task_spec": ("task_spec_not_found", "missing_evidence"),
    "policy": ("policy_not_found", "missing_evidence"),
    "adr": ("adr_not_found", "unknown"),
    "runbook": ("runbook_not_found", "missing_evidence"),
}

_NOTICE_CODE_LITERAL = Literal[
    "task_spec_not_declared",
    "task_spec_not_found",
    "task_title_missing",
    "task_owner_missing",
    "acceptance_criteria_missing",
    "policy_not_declared",
    "policy_not_found",
    "adr_not_declared",
    "adr_not_found",
    "runbook_not_found",
]


class IntakeCollectionError(Exception):
    """摄入收集失败的基类异常。"""


class IntakePathError(IntakeCollectionError):
    """仓库根或声明路径违反安全边界。"""


class IntakeFormatError(IntakeCollectionError):
    """文档内容或 frontmatter 不符合格式契约。"""


class IntakeChangedError(IntakeCollectionError):
    """声明文件在收集期间发生变化。"""


class IntakeDocument(BaseModel):
    """一份已摄入文档的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    kind: Literal["task_spec", "policy", "adr", "runbook"]
    path: str
    artifact_digest: str
    byte_size: int = Field(strict=True, ge=0)
    title: str | None = None
    owner: str | None = None
    version: str | None = None
    status: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    @field_validator("path", mode="before")
    @classmethod
    def _canonical_path(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("path must be a str")
        if normalize_repo_path(value) != value:
            raise ValueError("path must be canonical")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if not isinstance(value, str) or _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @field_validator("title", "owner", "version", "status")
    @classmethod
    def _reject_blank_optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _validate_criteria(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("criteria items must be nonempty strings")
            if item in seen:
                raise ValueError("criteria items must be unique")
            seen.add(item)
            result.append(item)
        return tuple(result)

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(
        cls, value: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        seen: set[str] = set()
        previous: str | None = None
        result: list[tuple[str, str]] = []
        for pair in value:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError("metadata items must be (key, value) pairs")
            key, item_value = pair
            if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
                raise ValueError("metadata keys must be lowercase snake_case")
            if not isinstance(item_value, str) or not item_value.strip():
                raise ValueError("metadata values must be nonempty")
            if key in seen:
                raise ValueError("metadata keys must be unique")
            if previous is not None and key <= previous:
                raise ValueError("metadata keys must be sorted")
            seen.add(key)
            previous = key
            result.append((key, item_value))
        return tuple(result)

    @model_validator(mode="after")
    def _criteria_only_for_task_spec(self) -> "IntakeDocument":
        if self.kind != "task_spec" and self.acceptance_criteria:
            raise ValueError(
                "acceptance criteria are only allowed for task_spec documents"
            )
        return self


class IntakeNotice(BaseModel):
    """一条摄入注意力的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    category: Literal["missing_evidence", "unknown"]
    code: _NOTICE_CODE_LITERAL
    path: str | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _canonical_path(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("path must be a str or None")
        if normalize_repo_path(value) != value:
            raise ValueError("path must be canonical")
        return value

    @model_validator(mode="after")
    def _code_semantics(self) -> "IntakeNotice":
        expected_category, requires_path = _NOTICE_SEMANTICS[self.code]
        if self.category != expected_category:
            raise ValueError(
                f"category for {self.code} must be {expected_category}"
            )
        if requires_path and self.path is None:
            raise ValueError(f"{self.code} requires a path")
        if not requires_path and self.path is not None:
            raise ValueError(f"{self.code} forbids a path")
        return self


class IntakeSnapshot(BaseModel):
    """摄入文档、注意力和 manifest 摘要的不可变快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject_digest: str
    documents: tuple[IntakeDocument, ...] = ()
    notices: tuple[IntakeNotice, ...] = ()
    task_digest: str | None = None
    task_present: bool = Field(strict=True)
    policy_count: int = Field(strict=True, ge=0)
    adr_count: int = Field(strict=True, ge=0)
    runbook_count: int = Field(strict=True, ge=0)
    manifest_artifact_digest: str
    complete: bool = Field(strict=True)
    collected_at: AwareDatetime

    @field_validator("subject_digest", "manifest_artifact_digest")
    @classmethod
    def _validate_sha256_digest(cls, value: str) -> str:
        if not isinstance(value, str) or _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "IntakeSnapshot":
        task_documents = [
            document for document in self.documents
            if document.kind == "task_spec"
        ]
        if len(task_documents) > 1:
            raise ValueError("at most one task_spec document is allowed")
        expected_task_digest = (
            task_documents[0].artifact_digest if task_documents else None
        )
        if self.task_digest != expected_task_digest:
            raise ValueError("task_digest must equal the task document digest")
        if self.task_present != (expected_task_digest is not None):
            raise ValueError("task_present must agree with task_digest")
        if self.policy_count != sum(
            document.kind == "policy" for document in self.documents
        ):
            raise ValueError("policy_count must match policy documents")
        if self.adr_count != sum(
            document.kind == "adr" for document in self.documents
        ):
            raise ValueError("adr_count must match adr documents")
        if self.runbook_count != sum(
            document.kind == "runbook" for document in self.documents
        ):
            raise ValueError("runbook_count must match runbook documents")
        paths = [document.path for document in self.documents]
        if len(set(paths)) != len(paths):
            raise ValueError("document paths must be unique")
        document_keys = [
            (_KIND_ORDER[document.kind], document.path)
            for document in self.documents
        ]
        if any(
            document_keys[index] >= document_keys[index + 1]
            for index in range(len(document_keys) - 1)
        ):
            raise ValueError(
                "documents must be sorted by kind order and then path"
            )
        notice_keys = [
            (notice.category, notice.code, notice.path or "")
            for notice in self.notices
        ]
        if any(
            notice_keys[index] >= notice_keys[index + 1]
            for index in range(len(notice_keys) - 1)
        ):
            raise ValueError(
                "notices must be sorted by category, code, and path"
            )
        if self.complete != (not any(
            notice.category == "missing_evidence" for notice in self.notices
        )):
            raise ValueError(
                "complete must be true iff no missing_evidence notice exists"
            )
        return self


class IntakeResult(BaseModel):
    """摄入快照及其确定性 Evidence 的不可变结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    snapshot: IntakeSnapshot
    evidence: Evidence

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "IntakeResult":
        if self.snapshot.subject_digest != self.evidence.subject_digest:
            raise ValueError("snapshot and evidence subject digests must match")
        if (
            self.snapshot.manifest_artifact_digest
            != self.evidence.artifact_digest
        ):
            raise ValueError(
                "snapshot and evidence artifact digests must match"
            )
        if self.evidence.kind != "intake_documents":
            raise ValueError("evidence kind must be intake_documents")
        if self.evidence.producer != "collector.intake":
            raise ValueError("evidence producer must be collector.intake")
        if self.evidence.trust_level != "deterministic":
            raise ValueError("evidence trust_level must be deterministic")
        expected_status = "success" if self.snapshot.complete else "truncated"
        if self.evidence.status != expected_status:
            raise ValueError(
                "evidence status must be success for complete snapshots "
                "and truncated for incomplete snapshots"
            )
        return self


class TaskPolicyCollector:
    """只读收集任务规范、策略、ADR 与运行手册（无构造参数）。"""

    def collect(
        self,
        repository_path: Path,
        *,
        subject_digest: str,
        artifact_store: ArtifactStore,
        task_path: str | None,
        policy_paths: tuple[str, ...] = (),
        adr_paths: tuple[str, ...] = (),
        runbook_paths: tuple[str, ...] = (),
        collected_at: datetime | None = None,
    ) -> IntakeResult:
        if not isinstance(repository_path, Path):
            raise TypeError("repository_path must be a pathlib.Path")
        if type(subject_digest) is not str:
            raise TypeError("subject_digest must be a str")
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")
        if task_path is not None and type(task_path) is not str:
            raise TypeError("task_path must be a str or None")
        for label, value in (
            ("policy_paths", policy_paths),
            ("adr_paths", adr_paths),
            ("runbook_paths", runbook_paths),
        ):
            if type(value) is not tuple:
                raise TypeError(f"{label} must be a tuple")
        if collected_at is None:
            collected_at = datetime.now(timezone.utc)
        elif not isinstance(collected_at, datetime):
            raise TypeError("collected_at must be a datetime")
        elif collected_at.tzinfo is None or collected_at.utcoffset() is None:
            raise ValueError("collected_at must be timezone-aware")
        if _SHA256_DIGEST_RE.fullmatch(subject_digest) is None:
            raise ValueError("subject_digest must be a lowercase sha256 digest")

        root = self._resolve_repository_root(repository_path)
        declared = self._validate_declared(
            task_path, policy_paths, adr_paths, runbook_paths
        )
        present, missing = self._inspect_present(root, declared)

        parsed = []
        for kind, path, raw, pre, digest in present:
            metadata, criteria = self._parse_document(kind, path, raw)
            parsed.append((kind, path, raw, pre, digest, metadata, criteria))

        for kind, path, raw, pre, digest, metadata, criteria in parsed:
            self._revalidate_file(root, path, pre, digest)

        documents = []
        for kind, path, raw, pre, digest, metadata, criteria in parsed:
            documents.append(
                self._build_document(kind, path, raw, digest, metadata, criteria)
            )
        documents.sort(key=lambda document: (_KIND_ORDER[document.kind], document.path))

        notices = []
        if task_path is None:
            notices.append(
                IntakeNotice(
                    schema_version="v1",
                    category="missing_evidence",
                    code="task_spec_not_declared",
                )
            )
        if not policy_paths:
            notices.append(
                IntakeNotice(
                    schema_version="v1",
                    category="missing_evidence",
                    code="policy_not_declared",
                )
            )
        if not adr_paths:
            notices.append(
                IntakeNotice(
                    schema_version="v1",
                    category="unknown",
                    code="adr_not_declared",
                )
            )
        for kind, path in declared:
            if path not in missing:
                continue
            code, category = _NOT_FOUND_RULES[kind]
            notices.append(
                IntakeNotice(
                    schema_version="v1",
                    category=category,
                    code=code,
                    path=path,
                )
            )
        task_document = next(
            (
                document for document in documents
                if document.kind == "task_spec"
            ),
            None,
        )
        if task_document is not None:
            if task_document.title is None:
                notices.append(
                    IntakeNotice(
                        schema_version="v1",
                        category="missing_evidence",
                        code="task_title_missing",
                        path=task_document.path,
                    )
                )
            if task_document.owner is None:
                notices.append(
                    IntakeNotice(
                        schema_version="v1",
                        category="missing_evidence",
                        code="task_owner_missing",
                        path=task_document.path,
                    )
                )
            if not task_document.acceptance_criteria:
                notices.append(
                    IntakeNotice(
                        schema_version="v1",
                        category="missing_evidence",
                        code="acceptance_criteria_missing",
                        path=task_document.path,
                    )
                )
        notices.sort(
            key=lambda notice: (notice.category, notice.code, notice.path or "")
        )
        notices_tuple = tuple(notices)

        policy_count = sum(
            document.kind == "policy" for document in documents
        )
        adr_count = sum(document.kind == "adr" for document in documents)
        runbook_count = sum(
            document.kind == "runbook" for document in documents
        )
        task_digest = (
            task_document.artifact_digest
            if task_document is not None
            else None
        )
        task_present = task_document is not None
        complete = not any(
            notice.category == "missing_evidence" for notice in notices_tuple
        )

        manifest = {
            "schema_version": "v1",
            "subject_digest": subject_digest,
            "documents": [
                document.model_dump(mode="json") for document in documents
            ],
            "notices": [
                notice.model_dump(mode="json") for notice in notices_tuple
            ],
            "task_digest": task_digest,
            "task_present": task_present,
            "policy_count": policy_count,
            "adr_count": adr_count,
            "runbook_count": runbook_count,
            "complete": complete,
            "limits": {
                "max_declared_paths": _MAX_DECLARED_PATHS,
                "max_file_bytes": _MAX_FILE_BYTES,
                "max_total_bytes": _MAX_TOTAL_BYTES,
                "max_frontmatter_bytes": _MAX_FRONTMATTER_BYTES,
                "max_frontmatter_items": _MAX_FRONTMATTER_ITEMS,
            },
        }
        encoded = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        manifest_digest = _sha256_bytes(encoded)

        raw_by_path = {path: raw for _, path, raw, _, _, _, _ in parsed}
        for document in documents:
            returned = artifact_store.put_bytes(raw_by_path[document.path])
            if returned != document.artifact_digest:
                raise IntakeCollectionError(
                    f"artifact digest mismatch for {document.path}"
                )
        returned_manifest = artifact_store.put_bytes(encoded)
        if returned_manifest != manifest_digest:
            raise IntakeCollectionError("manifest artifact digest mismatch")

        snapshot = IntakeSnapshot(
            schema_version="v1",
            subject_digest=subject_digest,
            documents=tuple(documents),
            notices=notices_tuple,
            task_digest=task_digest,
            task_present=task_present,
            policy_count=policy_count,
            adr_count=adr_count,
            runbook_count=runbook_count,
            manifest_artifact_digest=manifest_digest,
            complete=complete,
            collected_at=collected_at,
        )
        evidence = Evidence(
            schema_version="v1",
            evidence_id=(
                "ev_intake_"
                + hashlib.sha256(
                    (subject_digest + manifest_digest).encode("ascii")
                ).hexdigest()[:32]
            ),
            subject_digest=subject_digest,
            kind="intake_documents",
            producer="collector.intake",
            artifact_digest=manifest_digest,
            source_ref=f"intake_documents:{subject_digest}",
            status="success" if complete else "truncated",
            trust_level="deterministic",
            collected_at=collected_at,
        )
        return IntakeResult(
            schema_version="v1",
            snapshot=snapshot,
            evidence=evidence,
        )

    def probe_task_digest(
        self,
        repository_path: Path,
        *,
        task_path: str,
    ) -> str:
        if not isinstance(repository_path, Path):
            raise TypeError("repository_path must be a pathlib.Path")
        if type(task_path) is not str:
            raise TypeError("task_path must be a str")

        root = self._resolve_repository_root(repository_path)
        validated_task_path = self._validate_path(task_path)
        present, missing = self._inspect_present(
            root,
            [("task_spec", validated_task_path)],
        )
        if missing or not present:
            raise IntakePathError(
                f"task path must be an existing regular file: "
                f"{validated_task_path}"
            )

        _, inspected_path, _, pre, digest = present[0]
        self._revalidate_file(root, inspected_path, pre, digest)
        return digest

    def _resolve_repository_root(self, repository_path: Path) -> Path:
        try:
            stat_result = repository_path.lstat()
        except FileNotFoundError as exc:
            raise IntakePathError(
                "repository path must be an existing directory"
            ) from exc
        except OSError as exc:
            raise IntakePathError(
                f"repository path cannot be inspected: {exc}"
            ) from exc
        if stat.S_ISLNK(stat_result.st_mode):
            raise IntakePathError("repository path must not be a symlink")
        if not stat.S_ISDIR(stat_result.st_mode):
            raise IntakePathError("repository path must be a directory")
        return repository_path

    def _validate_declared(
        self,
        task_path: str | None,
        policy_paths: tuple[str, ...],
        adr_paths: tuple[str, ...],
        runbook_paths: tuple[str, ...],
    ) -> list[tuple[str, str]]:
        declared: list[tuple[str, str]] = []
        if task_path is not None:
            declared.append(("task_spec", self._validate_path(task_path)))
        for kind, paths in (
            ("policy", policy_paths),
            ("adr", adr_paths),
            ("runbook", runbook_paths),
        ):
            for path in paths:
                declared.append((kind, self._validate_path(path)))
        seen: set[str] = set()
        for kind, path in declared:
            if path in seen:
                raise IntakePathError(
                    f"declared path repeated across kinds: {path}"
                )
            seen.add(path)
        if len(declared) > _MAX_DECLARED_PATHS:
            raise IntakePathError(
                f"declared paths exceed {_MAX_DECLARED_PATHS}"
            )
        return declared

    @staticmethod
    def _validate_path(value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("declared path must be a str")
        try:
            if normalize_repo_path(value) != value:
                raise IntakePathError(
                    f"declared path must be canonical: {value}"
                )
        except (TypeError, ValueError) as exc:
            raise IntakePathError(
                f"declared path is invalid: {value}"
            ) from exc
        if not value.endswith(".md"):
            raise IntakePathError(
                f"declared path must end in lowercase .md: {value}"
            )
        return value

    def _inspect_present(
        self,
        root: Path,
        declared: list[tuple[str, str]],
    ) -> tuple[list[tuple[str, str, bytes, tuple, str]], set[str]]:
        present: list[tuple[str, str, bytes, tuple, str]] = []
        missing: set[str] = set()
        total_bytes = 0
        for kind, path in declared:
            parts = path.split("/")
            current = root
            path_missing = False
            for index, part in enumerate(parts):
                current = current / part
                try:
                    stat_result = current.lstat()
                except FileNotFoundError:
                    path_missing = True
                    break
                except OSError as exc:
                    raise IntakePathError(
                        f"cannot inspect declared path: {path}"
                    ) from exc
                if stat.S_ISLNK(stat_result.st_mode):
                    raise IntakePathError(
                        f"declared path must not traverse symlinks: {path}"
                    )
                if index < len(parts) - 1:
                    if not stat.S_ISDIR(stat_result.st_mode):
                        raise IntakePathError(
                            "declared path component is not a directory: "
                            f"{path}"
                        )
                else:
                    if not stat.S_ISREG(stat_result.st_mode):
                        raise IntakePathError(
                            f"declared path must be a regular file: {path}"
                        )
                    if stat_result.st_size > _MAX_FILE_BYTES:
                        raise IntakePathError(
                            f"declared file exceeds {_MAX_FILE_BYTES} bytes: "
                            f"{path}"
                        )
                    pre = _lstat_key(stat_result)
                    try:
                        raw = _read_regular_file(current)
                    except IntakePathError as exc:
                        try:
                            check_stat = current.lstat()
                        except FileNotFoundError:
                            raise IntakeChangedError(
                                "declared file changed during collection: "
                                f"{path}"
                            ) from exc
                        if not stat.S_ISREG(check_stat.st_mode):
                            raise IntakeChangedError(
                                "declared file changed during collection: "
                                f"{path}"
                            ) from exc
                        raise IntakePathError(
                            f"cannot read declared file: {path}"
                        ) from exc
                    try:
                        post_stat = current.lstat()
                    except OSError as exc:
                        raise IntakeChangedError(
                            f"declared file changed during collection: {path}"
                        ) from exc
                    if _lstat_key(post_stat) != pre:
                        raise IntakeChangedError(
                            f"declared file changed during collection: {path}"
                        )
                    if len(raw) > _MAX_FILE_BYTES:
                        raise IntakePathError(
                            f"declared file exceeds {_MAX_FILE_BYTES} bytes: "
                            f"{path}"
                        )
                    total_bytes += len(raw)
                    if total_bytes > _MAX_TOTAL_BYTES:
                        raise IntakePathError(
                            f"declared files exceed {_MAX_TOTAL_BYTES} total "
                            "bytes"
                        )
                    present.append(
                        (kind, path, raw, pre, _sha256_bytes(raw))
                    )
            if path_missing:
                missing.add(path)
        return present, missing

    def _revalidate_file(
        self,
        root: Path,
        path: str,
        pre: tuple,
        digest: str,
    ) -> None:
        full = root / path
        try:
            current = full.lstat()
        except FileNotFoundError as exc:
            raise IntakeChangedError(
                f"declared file disappeared during collection: {path}"
            ) from exc
        except OSError as exc:
            raise IntakeChangedError(
                f"cannot re-inspect declared file: {path}"
            ) from exc
        if _lstat_key(current) != pre:
            raise IntakeChangedError(
                f"declared file changed during collection: {path}"
            )
        try:
            final_raw = _read_regular_file(full)
        except IntakePathError as exc:
            raise IntakeChangedError(
                f"declared file changed during collection: {path}"
            ) from exc
        if _sha256_bytes(final_raw) != digest:
            raise IntakeChangedError(
                f"declared file content changed during collection: {path}"
            )

    def _parse_document(
        self,
        kind: str,
        path: str,
        raw: bytes,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        if b"\x00" in raw:
            raise IntakeFormatError(
                f"declared document contains NUL bytes: {path}"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntakeFormatError(
                f"declared document is not valid UTF-8: {path}"
            ) from exc
        metadata: dict[str, str] = {}
        body_text = text
        first_line = raw.split(b"\n", 1)[0].rstrip(b"\r")
        if first_line == b"---":
            frontmatter_end = _frontmatter_end(raw)
            if frontmatter_end is None:
                raise IntakeFormatError(
                    f"frontmatter terminator is missing: {path}"
                )
            if frontmatter_end > _MAX_FRONTMATTER_BYTES:
                raise IntakeFormatError(
                    f"frontmatter exceeds {_MAX_FRONTMATTER_BYTES} bytes: "
                    f"{path}"
                )
            block_text = raw[:frontmatter_end].decode("utf-8")
            item_lines = []
            for line in block_text.split("\n")[1:]:
                if line.rstrip("\r") == "---":
                    break
                item_lines.append(line)
            for line in item_lines:
                self._parse_frontmatter_line(path, line, metadata)
                if len(metadata) > _MAX_FRONTMATTER_ITEMS:
                    raise IntakeFormatError(
                        f"frontmatter exceeds {_MAX_FRONTMATTER_ITEMS} items: "
                        f"{path}"
                    )
            body_text = raw[frontmatter_end:].decode("utf-8")
        criteria = (
            self._scan_criteria(path, body_text)
            if kind == "task_spec"
            else ()
        )
        return metadata, criteria

    @staticmethod
    def _parse_frontmatter_line(
        path: str,
        line: str,
        metadata: dict[str, str],
    ) -> None:
        if not line:
            return
        if line[0] in " \t":
            raise IntakeFormatError(
                f"frontmatter lines must not be indented: {path}"
            )
        if line.startswith("#"):
            raise IntakeFormatError(
                f"frontmatter comment lines are not allowed: {path}"
            )
        key, separator, value = line.partition(":")
        if not separator:
            raise IntakeFormatError(
                f"frontmatter items must contain a colon: {path}"
            )
        if _KEY_RE.fullmatch(key) is None:
            raise IntakeFormatError(
                f"frontmatter keys must be lowercase snake_case: {path}"
            )
        stripped = value.strip()
        if not stripped:
            raise IntakeFormatError(
                f"frontmatter values must not be empty: {path}"
            )
        if stripped in _MULTILINE_SCALARS:
            raise IntakeFormatError(
                f"multiline frontmatter scalars are not allowed: {path}"
            )
        if stripped[0] in "[{":
            raise IntakeFormatError(
                f"frontmatter values must be single-line scalars: {path}"
            )
        if key in metadata:
            raise IntakeFormatError(
                f"frontmatter keys must be unique: {path}"
            )
        metadata[key] = stripped

    @staticmethod
    def _scan_criteria(path: str, body_text: str) -> tuple[str, ...]:
        criteria: list[str] = []
        seen: set[str] = set()
        for line in body_text.split("\n"):
            stripped_line = line.rstrip("\r")
            match = _CHECKBOX_RE.match(stripped_line)
            if match is None:
                continue
            rest = match.group("rest")
            if rest and not rest.startswith(" "):
                continue
            text = rest.strip()
            if not text:
                raise IntakeFormatError(
                    f"task checkbox criteria must not be empty: {path}"
                )
            if text in seen:
                raise IntakeFormatError(
                    f"task checkbox criteria must be unique: {path}"
                )
            seen.add(text)
            criteria.append(text)
        return tuple(criteria)

    @staticmethod
    def _build_document(
        kind: str,
        path: str,
        raw: bytes,
        digest: str,
        metadata: dict[str, str],
        criteria: tuple[str, ...],
    ) -> IntakeDocument:
        metadata_items = tuple(
            (key, metadata[key]) for key in sorted(metadata)
        )
        return IntakeDocument(
            schema_version="v1",
            kind=kind,
            path=path,
            artifact_digest=digest,
            byte_size=len(raw),
            title=metadata.get("title"),
            owner=metadata.get("owner"),
            version=metadata.get("version"),
            status=metadata.get("status"),
            acceptance_criteria=criteria if kind == "task_spec" else (),
            metadata=metadata_items,
        )


def _frontmatter_end(raw: bytes) -> int | None:
    newline = raw.find(b"\n")
    if newline == -1:
        return None
    cursor = newline + 1
    while cursor < len(raw):
        next_newline = raw.find(b"\n", cursor)
        if next_newline == -1:
            if raw[cursor:].rstrip(b"\r") == b"---":
                return len(raw)
            return None
        if raw[cursor:next_newline].rstrip(b"\r") == b"---":
            return next_newline + 1
        cursor = next_newline + 1
    return None


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise IntakePathError("cannot open declared file") from exc
    try:
        with os.fdopen(fd, "rb") as fh:
            return fh.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise IntakePathError("cannot read declared file") from exc


def _lstat_key(stat_result) -> tuple:
    return (
        stat_result.st_mode,
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


_TASK_POLICY_KIND = "task_policy_adr"
_TASK_POLICY_PRODUCER = "collector.task_policy_adr"


def _task_policy_limits() -> dict[str, int]:
    return {
        "max_declared_paths": _MAX_DECLARED_PATHS,
        "max_file_bytes": _MAX_FILE_BYTES,
        "max_total_bytes": _MAX_TOTAL_BYTES,
        "max_frontmatter_bytes": _MAX_FRONTMATTER_BYTES,
        "max_frontmatter_items": _MAX_FRONTMATTER_ITEMS,
    }


def _task_policy_manifest_payload(
    snapshot: IntakeSnapshot,
    intake_evidence: Evidence | None = None,
) -> dict[str, object]:
    """Build the path/digest-only contract for the dedicated collector.

    The original ``intake_documents`` Evidence remains the owner of the full
    intake manifest.  This second manifest is deliberately a separate,
    deterministic projection for the task/policy/ADR collector and never
    contains document bodies.
    """

    if type(snapshot) is not IntakeSnapshot:
        raise TypeError("snapshot must be an exact IntakeSnapshot")
    if intake_evidence is None:
        intake_evidence = Evidence(
            schema_version="v1",
            evidence_id=(
                "ev_intake_"
                + hashlib.sha256(
                    (
                        snapshot.subject_digest
                        + snapshot.manifest_artifact_digest
                    ).encode("ascii")
                ).hexdigest()[:32]
            ),
            subject_digest=snapshot.subject_digest,
            kind="intake_documents",
            producer="collector.intake",
            artifact_digest=snapshot.manifest_artifact_digest,
            source_ref=f"intake_documents:{snapshot.subject_digest}",
            status="success" if snapshot.complete else "truncated",
            trust_level="deterministic",
            collected_at=snapshot.collected_at,
        )
    if type(intake_evidence) is not Evidence:
        raise TypeError("intake_evidence must be an exact Evidence")
    if (
        intake_evidence.subject_digest != snapshot.subject_digest
        or intake_evidence.kind != "intake_documents"
        or intake_evidence.producer != "collector.intake"
        or intake_evidence.artifact_digest != snapshot.manifest_artifact_digest
        or intake_evidence.source_ref
        != f"intake_documents:{snapshot.subject_digest}"
        or intake_evidence.trust_level != "deterministic"
    ):
        raise IntakeCollectionError("intake Evidence binding is invalid")

    documents = tuple(
        document
        for document in snapshot.documents
        if document.kind in {"task_spec", "policy", "adr"}
    )
    notices = tuple(snapshot.notices)
    adr_paths = tuple(
        document.path for document in documents if document.kind == "adr"
    )
    adr_missing = any(notice.code == "adr_not_found" for notice in notices)
    complete = (
        snapshot.task_present
        and snapshot.policy_count > 0
        and not any(
            notice.category == "missing_evidence" for notice in notices
        )
        and not adr_missing
    )

    document_states: dict[str, dict[str, object]] = {}
    for kind in ("task_spec", "policy", "adr"):
        matching = tuple(document for document in documents if document.kind == kind)
        if kind == "adr" and adr_missing:
            status = "missing"
            state_complete = False
            empty = False
            omissions = [
                notice.code for notice in notices if notice.code == "adr_not_found"
            ]
        elif kind == "adr" and not adr_paths:
            status = "not_declared"
            state_complete = False
            empty = True
            omissions = []
        else:
            status = "success" if matching else "not_declared"
            state_complete = bool(matching)
            empty = False
            omissions = [] if matching else [f"{kind}_not_declared"]
        document_states[kind] = {
            "kind": kind,
            "status": status,
            "complete": state_complete,
            "empty": empty,
            "omissions": omissions,
            "subject_digest": snapshot.subject_digest,
            "items": [
                document.model_dump(mode="json") for document in matching
            ],
            "adr_paths": list(adr_paths) if kind == "adr" else [],
        }

    return {
        "schema_version": "v1",
        "subject_digest": snapshot.subject_digest,
        "intake_evidence": {
            "evidence_id": intake_evidence.evidence_id,
            "kind": intake_evidence.kind,
            "producer": intake_evidence.producer,
            "subject_digest": intake_evidence.subject_digest,
            "artifact_digest": intake_evidence.artifact_digest,
            "source_ref": intake_evidence.source_ref,
            "status": intake_evidence.status,
            "trust_level": intake_evidence.trust_level,
        },
        "intake_manifest_digest": snapshot.manifest_artifact_digest,
        "documents": [
            document.model_dump(mode="json") for document in documents
        ],
        "document_states": document_states,
        "notices": [notice.model_dump(mode="json") for notice in notices],
        "task_digest": snapshot.task_digest,
        "task_present": snapshot.task_present,
        "policy_count": snapshot.policy_count,
        "adr_count": snapshot.adr_count,
        "adr_paths": list(adr_paths),
        "complete": complete,
        "limits": _task_policy_limits(),
    }


def _build_task_policy_adr_evidence(
    result: IntakeResult,
    *,
    artifact_store: ArtifactStore,
) -> Evidence:
    """Persist and return the independent task/policy/ADR Evidence.

    Every declared task, policy, and ADR document is reread from CAS and
    checked against its typed path, digest, and byte size before the dedicated
    Evidence can be marked successful.  A declared-but-missing ADR is kept
    explicit and makes this dedicated Evidence truncated; an empty ADR list is
    an explicit successful ``not_declared`` state when task and policy pass.
    """

    if type(result) is not IntakeResult:
        raise TypeError("result must be an exact IntakeResult")
    if type(artifact_store) is not ArtifactStore:
        raise TypeError("artifact_store must be an exact ArtifactStore")
    snapshot = result.snapshot
    if result.evidence.artifact_digest != snapshot.manifest_artifact_digest:
        raise IntakeCollectionError("intake manifest Evidence binding is invalid")

    expected_manifest = {
        "schema_version": "v1",
        "subject_digest": snapshot.subject_digest,
        "documents": [
            document.model_dump(mode="json")
            for document in snapshot.documents
        ],
        "notices": [notice.model_dump(mode="json") for notice in snapshot.notices],
        "task_digest": snapshot.task_digest,
        "task_present": snapshot.task_present,
        "policy_count": snapshot.policy_count,
        "adr_count": snapshot.adr_count,
        "runbook_count": snapshot.runbook_count,
        "complete": snapshot.complete,
        "limits": _task_policy_limits(),
    }
    try:
        raw_manifest = artifact_store.get_bytes(
            snapshot.manifest_artifact_digest
        )
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except Exception as exc:
        raise IntakeCollectionError("intake manifest CAS read failed") from exc
    if manifest != expected_manifest:
        raise IntakeCollectionError("intake manifest CAS binding is invalid")

    for document in snapshot.documents:
        try:
            raw_document = artifact_store.get_bytes(document.artifact_digest)
        except Exception as exc:
            raise IntakeCollectionError("intake document CAS read failed") from exc
        if len(raw_document) != document.byte_size or _sha256_bytes(raw_document) != document.artifact_digest:
            raise IntakeCollectionError("intake document CAS binding is invalid")

    payload = _task_policy_manifest_payload(snapshot, result.evidence)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    try:
        artifact_digest = artifact_store.put_bytes(encoded)
    except Exception as exc:
        raise IntakeCollectionError("task policy Evidence persistence failed") from exc
    if artifact_digest != _sha256_bytes(encoded):
        raise IntakeCollectionError("task policy Evidence digest mismatch")
    return Evidence(
        schema_version="v1",
        evidence_id=(
            "ev_task_policy_adr_"
            + hashlib.sha256(
                (snapshot.subject_digest + artifact_digest).encode("ascii")
            ).hexdigest()[:32]
        ),
        subject_digest=snapshot.subject_digest,
        kind=_TASK_POLICY_KIND,
        producer=_TASK_POLICY_PRODUCER,
        artifact_digest=artifact_digest,
        source_ref=f"{_TASK_POLICY_KIND}:{snapshot.subject_digest}",
        status="success" if payload["complete"] else "truncated",
        trust_level="deterministic",
        collected_at=result.evidence.collected_at,
    )
