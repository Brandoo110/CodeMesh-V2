"""Content-addressed Assurance Evidence Bundle v1.

The bundle is the local-substitutable seam for publishing an already-authoritative
Case.  It deliberately contains no database access, provider call, or GitHub
transport.  A caller gives this module a read-only evidence directory and gets
one canonical, self-verifying JSON document back.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


_SCHEMA_VERSION = "v1"
_BUNDLE_TYPE = "AssuranceEvidenceBundle"
_ORIGIN = "local_authoritative_bundle"
_DEFAULT_PREFIX = "mvp-08f-remediation-post-fix-03-"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[^/\s?#]+/[^/\s?#]+$")
_ARTIFACT_FILE_RE = re.compile(r"^sha256_([0-9a-f]{64})$")
_INDEX_FILE_RE = re.compile(r"^ev_[A-Za-z0-9_-]+-index\.json$")
_TRANSPORT_REF_RE = re.compile(
    r"^refs/heads/codex/evidence-v2/([0-9a-f]{40})/([0-9a-f]{40})$"
)
_MAX_OBJECT_BYTES = 256 * 1024
_MAX_BUNDLE_BYTES = 900 * 1024
_MAX_OBJECTS = 256
_MAX_NESTING = 32

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|secret|password|cookie|authorization)"
    r"\s*[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})"
)
_PLACEHOLDER_VALUES = {
    "absolute/path/to/codemesh_workspace",
    "changeme",
    "example",
    "example-token",
    "not-configured",
    "placeholder",
    "replace-me",
    "your-token",
}


class BundleError(ValueError):
    """The evidence directory or bundle violates the v1 contract."""


@dataclass(frozen=True)
class BuiltEvidenceBundle:
    """Canonical bundle bytes and safe transport facts."""

    bundle_bytes: bytes
    bundle_digest: str
    transport_id: str
    transport_ref: str
    target_pr: int
    case_id: str
    run_id: str
    subject_digest: str
    passport_digest: str
    producer_head: str
    transport_head: str


@dataclass(frozen=True)
class VerifiedEvidenceBundle:
    """A verified immutable bundle document."""

    document: dict[str, Any]
    bundle_digest: str


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BundleError("value is not canonical JSON") from exc


def _canonical_json_bytes(value: object) -> bytes:
    return _canonical_json(value) + b"\n"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("JSON contains duplicate keys")
        result[key] = value
    return result


def _parse_json(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise BundleError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise BundleError(f"{label} must be a JSON object")
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Return the v1 canonical JSON representation with one trailing newline."""

    return _canonical_json_bytes(value)


def _scan_secret(data: bytes, *, label: str) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise BundleError(f"suspected secret in {label}")
    for match in _ASSIGNMENT_SECRET_RE.finditer(text):
        value = match.group(1).strip().lower().rstrip(",}")
        if value not in _PLACEHOLDER_VALUES and not value.startswith("/absolute/path"):
            raise BundleError(f"suspected secret in {label}")


def _real_directory(path: Path, *, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BundleError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BundleError(f"{label} must be a real directory")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BundleError(f"{label} is unavailable") from exc
    if resolved != path.absolute().resolve() and path.is_symlink():
        raise BundleError(f"{label} must not be a symlink")
    return resolved


def _read_stable(path: Path, *, label: str, max_bytes: int) -> bytes:
    """Read one regular file and reject symlinks and metadata races."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise BundleError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise BundleError(f"{label} must be a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BundleError(f"{label} could not be opened safely") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise BundleError(f"TOCTOU change detected for {label}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise BundleError(f"{label} exceeds size limit")
        after_open = os.fstat(fd)
    except BundleError:
        raise
    except OSError as exc:
        raise BundleError(f"{label} could not be read") from exc
    finally:
        os.close(fd)
    try:
        after = path.lstat()
    except OSError as exc:
        raise BundleError(f"TOCTOU change detected for {label}") from exc
    if stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode):
        raise BundleError(f"TOCTOU change detected for {label}")
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        or (after_open.st_dev, after_open.st_ino, after_open.st_size)
        != (opened.st_dev, opened.st_ino, opened.st_size)
    ):
        raise BundleError(f"TOCTOU change detected for {label}")
    data = b"".join(chunks)
    _scan_secret(data, label=label)
    return data


def _require_text(value: object, *, field: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise BundleError(f"{field} must be nonblank text")
    return value


def _require_digest(value: object, *, field: str) -> str:
    text = _require_text(value, field=field)
    if _SHA256_RE.fullmatch(text) is None:
        raise BundleError(f"{field} must be a lowercase sha256 digest")
    return text


def _require_sha1(value: object, *, field: str) -> str:
    text = _require_text(value, field=field)
    if _SHA1_RE.fullmatch(text) is None:
        raise BundleError(f"{field} must be a lowercase 40-character SHA")
    return text


def transport_ref_for(*, producer_head: str, transport_head: str) -> str:
    """Return the immutable evidence ref for both producer and transport heads."""

    producer = _require_sha1(producer_head, field="producer_head")
    transport = _require_sha1(transport_head, field="transport_head")
    return f"refs/heads/codex/evidence-v2/{producer}/{transport}"


def parse_transport_ref(ref: object) -> tuple[str, str] | None:
    """Parse a dual-version evidence ref; legacy or malformed refs are not valid."""

    if type(ref) is not str:
        return None
    match = _TRANSPORT_REF_RE.fullmatch(ref)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _require_repository(value: object) -> str:
    text = _require_text(value, field="repository")
    if _REPOSITORY_RE.fullmatch(text) is None:
        raise BundleError("repository must be owner/repository")
    return text


def _require_pr(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise BundleError("target_pr must be a positive integer")
    return value


def _stable_passport_digest(passport: Mapping[str, object]) -> str:
    stable = json.loads(_canonical_json(passport).decode("utf-8"))
    freshness = stable.get("freshness")
    if isinstance(freshness, dict):
        freshness.pop("checked_at", None)
    return _digest(_canonical_json(stable))


def _transport_id(
    *,
    repository: str,
    target_pr: int,
    case_id: str,
    run_id: str,
    subject_digest: str,
    producer_head: str,
) -> str:
    seed = {
        "schema_version": _SCHEMA_VERSION,
        "repository": repository,
        "target_pr": target_pr,
        "case_id": case_id,
        "run_id": run_id,
        "subject_digest": subject_digest,
        "producer_head": producer_head,
    }
    return "transport-" + hashlib.sha256(_canonical_json(seed)).hexdigest()[:24]


def _json_object(
    *,
    name: str,
    value: Mapping[str, object],
    role: str,
    max_object_bytes: int,
) -> tuple[str, dict[str, object]]:
    data = _canonical_json_bytes(value)
    return _object_record(
        name=name,
        data=data,
        media_type="application/json",
        role=role,
        max_object_bytes=max_object_bytes,
    )


def _object_record(
    *,
    name: str,
    data: bytes,
    media_type: str,
    role: str,
    max_object_bytes: int,
) -> tuple[str, dict[str, object]]:
    if type(max_object_bytes) is not int or max_object_bytes <= 0:
        raise BundleError("max_object_bytes must be positive")
    if len(data) > max_object_bytes:
        raise BundleError(f"object {name} exceeds size limit")
    _scan_secret(data, label=name)
    digest = _digest(data)
    return digest, {
        "digest": digest,
        "size": len(data),
        "media_type": media_type,
        "role": role,
        "name": name,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def _selected_paths(root: Path, prefix: str) -> dict[str, Path]:
    suffixes = {
        "case": "authoritative-case.json",
        "run": "response.json",
        "passport": "passport.json",
        "passport_markdown": "passport.md",
    }
    selected: dict[str, Path] = {}
    for key, suffix in suffixes.items():
        path = root / f"{prefix}{suffix}"
        if not path.exists():
            raise BundleError(f"missing authoritative {key} file")
        selected[key] = path
    return selected


def _validate_core_payloads(
    *,
    case: Mapping[str, object],
    run: Mapping[str, object],
    passport: Mapping[str, object],
    case_id: str,
) -> tuple[str, str, list[dict[str, object]]]:
    if case.get("schema_version") != "v1":
        raise BundleError("Case schema_version is unsupported")
    if case.get("case_id") != case_id:
        raise BundleError("Case case_id did not match requested case")
    subject_digest = _require_digest(case.get("subject_digest"), field="subject_digest")
    if passport.get("case_id") != case_id or passport.get("subject_digest") != subject_digest:
        raise BundleError("Case and Passport bindings did not match")
    if passport.get("schema") != "codemesh.assurance.passport.v1":
        raise BundleError("Passport schema is unsupported")
    case_view = run.get("case_view")
    receipt = run.get("receipt")
    if not isinstance(case_view, Mapping) or not isinstance(receipt, Mapping):
        raise BundleError("Run response is missing case_view or receipt")
    if (
        case_view.get("case_id") != case_id
        or case_view.get("subject_digest") != subject_digest
        or receipt.get("case_id", case_id) != case_id
        or receipt.get("subject_digest", subject_digest) != subject_digest
    ):
        raise BundleError("Run bindings did not match Case")
    run_id = _require_text(
        receipt.get("run_id") or case.get("metadata", {}).get("run_id")
        if isinstance(case.get("metadata"), Mapping)
        else receipt.get("run_id"),
        field="run_id",
    )
    if isinstance(case.get("metadata"), Mapping) and case["metadata"].get("run_id") not in {
        None,
        run_id,
    }:
        raise BundleError("Case metadata run_id did not match Run")
    case_evidence = case.get("evidence")
    passport_evidence = passport.get("evidence")
    if not isinstance(case_evidence, list) or not isinstance(passport_evidence, list):
        raise BundleError("Case and Passport evidence must be lists")
    if len(case_evidence) != len(passport_evidence):
        raise BundleError("Case and Passport evidence counts did not match")
    passport_by_id: dict[str, Mapping[str, object]] = {}
    for item in passport_evidence:
        if not isinstance(item, Mapping):
            raise BundleError("Passport evidence entry is invalid")
        evidence_id = _require_text(item.get("evidence_id"), field="evidence_id")
        if evidence_id in passport_by_id:
            raise BundleError("Passport evidence IDs are not unique")
        passport_by_id[evidence_id] = item
    evidence_rows: list[dict[str, object]] = []
    case_ids: set[str] = set()
    for item in case_evidence:
        if not isinstance(item, Mapping):
            raise BundleError("Case evidence entry is invalid")
        evidence_id = _require_text(item.get("evidence_id"), field="evidence_id")
        if evidence_id in case_ids:
            raise BundleError("Case evidence IDs are not unique")
        case_ids.add(evidence_id)
        passport_item = passport_by_id.get(evidence_id)
        artifact_digest = _require_digest(item.get("artifact_digest"), field="artifact_digest")
        if (
            passport_item is None
            or passport_item.get("artifact_digest") != artifact_digest
        ):
            raise BundleError("Case and Passport evidence bindings did not match")
        row = dict(item)
        row["artifact_digest"] = artifact_digest
        evidence_rows.append(row)
    if set(passport_by_id) != case_ids:
        raise BundleError("Case and Passport evidence IDs did not match")
    return run_id, subject_digest, evidence_rows


def _validate_index(index: Mapping[str, object], *, case_id: str, evidence_id: str) -> list[dict[str, object]]:
    if index.get("schema_version") != "v1" or index.get("case_id") != case_id:
        raise BundleError("Evidence index binding did not match Case")
    if index.get("evidence_id") != evidence_id:
        raise BundleError("Evidence index ID did not match its filename")
    artifacts = index.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise BundleError("Evidence index artifacts are missing")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise BundleError("Evidence index artifact is invalid")
        digest = _require_digest(item.get("digest"), field="artifact digest")
        if digest in seen:
            raise BundleError("Evidence index artifact digests are not unique")
        seen.add(digest)
        byte_size = item.get("byte_size")
        if type(byte_size) is not int or byte_size < 0:
            raise BundleError("Evidence artifact byte_size is invalid")
        label = _require_text(item.get("label"), field="artifact label")
        media_type = _require_text(item.get("media_type"), field="artifact media_type")
        role = _require_text(item.get("role"), field="artifact role")
        normalized.append({
            "digest": digest,
            "byte_size": byte_size,
            "media_type": media_type,
            "role": role,
            "label": label,
            "path": item.get("path"),
            "command_id": item.get("command_id"),
            "stream": item.get("stream"),
        })
    return normalized


def _build_document(
    evidence_root: Path,
    *,
    case_id: str,
    repository: str,
    target_pr: int,
    producer_head: str,
    transport_head: str,
    prefix: str,
    max_object_bytes: int,
) -> tuple[dict[str, Any], str, str, str, str, str, str]:
    root = _real_directory(Path(evidence_root), label="evidence root")
    case_id = _require_text(case_id, field="case_id")
    repository = _require_repository(repository)
    target_pr = _require_pr(target_pr)
    producer_head = _require_sha1(producer_head, field="producer_head")
    transport_head = _require_sha1(transport_head, field="transport_head")
    if type(prefix) is not str or not prefix or "/" in prefix or ".." in prefix:
        raise BundleError("evidence prefix is invalid")
    selected = _selected_paths(root, prefix)
    payload_bytes = {
        key: _read_stable(path, label=key, max_bytes=max_object_bytes)
        for key, path in selected.items()
    }
    case = _parse_json(payload_bytes["case"], label="Case")
    run = _parse_json(payload_bytes["run"], label="Run")
    passport = _parse_json(payload_bytes["passport"], label="Passport")
    passport_markdown = payload_bytes["passport_markdown"]
    run_id, subject_digest, evidence_rows = _validate_core_payloads(
        case=case,
        run=run,
        passport=passport,
        case_id=case_id,
    )
    passport_digest = _stable_passport_digest(passport)
    transport_id = _transport_id(
        repository=repository,
        target_pr=target_pr,
        case_id=case_id,
        run_id=run_id,
        subject_digest=subject_digest,
        producer_head=producer_head,
    )
    transport_ref = transport_ref_for(
        producer_head=producer_head,
        transport_head=transport_head,
    )

    objects_by_digest: dict[str, dict[str, object]] = {}
    references: set[str] = set()

    def add_object(
        *,
        name: str,
        data: bytes,
        media_type: str,
        role: str,
    ) -> str:
        digest, record = _object_record(
            name=name,
            data=data,
            media_type=media_type,
            role=role,
            max_object_bytes=max_object_bytes,
        )
        existing = objects_by_digest.get(digest)
        if existing is not None and existing != record:
            raise BundleError("same content address has conflicting metadata")
        objects_by_digest[digest] = record
        references.add(digest)
        return digest

    case_object = add_object(
        name="case.json",
        data=_canonical_json_bytes(case),
        media_type="application/json",
        role="case",
    )
    run_object = add_object(
        name="run.json",
        data=_canonical_json_bytes(run),
        media_type="application/json",
        role="run",
    )
    passport_object = add_object(
        name="passport.json",
        data=_canonical_json_bytes(passport),
        media_type="application/json",
        role="passport",
    )
    passport_markdown_object = add_object(
        name="passport.md",
        data=passport_markdown,
        media_type="text/markdown",
        role="passport_markdown",
    )

    artifact_root = _real_directory(root / f"{prefix}artifacts", label="artifact root")
    index_files: dict[str, Path] = {}
    artifact_files: dict[str, Path] = {}
    for path in sorted(artifact_root.iterdir(), key=lambda item: item.name):
        try:
            info = path.lstat()
        except OSError as exc:
            raise BundleError("artifact root changed while enumerating") from exc
        if stat.S_ISLNK(info.st_mode):
            raise BundleError("artifact root contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            raise BundleError("artifact root contains an unexpected directory")
        if not stat.S_ISREG(info.st_mode):
            raise BundleError("artifact root contains a non-regular entry")
        if _INDEX_FILE_RE.fullmatch(path.name):
            index_files[path.name] = path
        elif _ARTIFACT_FILE_RE.fullmatch(path.name):
            artifact_files[path.name] = path
        else:
            raise BundleError("artifact root contains an extra file")

    expected_evidence = {str(item.get("evidence_id")) for item in evidence_rows}
    indexes: dict[str, tuple[dict[str, object], str]] = {}
    expected_artifacts: dict[str, dict[str, object]] = {}
    for filename, path in index_files.items():
        data = _read_stable(path, label=f"evidence index {filename}", max_bytes=max_object_bytes)
        index = _parse_json(data, label=f"evidence index {filename}")
        evidence_id = _require_text(index.get("evidence_id"), field="evidence_id")
        if evidence_id not in expected_evidence or evidence_id in indexes:
            raise BundleError("evidence indexes contain missing or extra evidence")
        artifacts = _validate_index(index, case_id=case_id, evidence_id=evidence_id)
        index_object = add_object(
            name=f"evidence-index/{filename}",
            data=_canonical_json_bytes(index),
            media_type="application/json",
            role="evidence_index",
        )
        indexes[evidence_id] = (index, index_object)
        for artifact in artifacts:
            digest = str(artifact["digest"])
            existing = expected_artifacts.get(digest)
            if existing is not None and existing != artifact:
                raise BundleError("artifact metadata differs across evidence indexes")
            expected_artifacts[digest] = artifact
    if set(indexes) != expected_evidence:
        raise BundleError("evidence indexes are missing or extra")

    actual_digests = {
        "sha256:" + match.group(1): path
        for name, path in artifact_files.items()
        if (match := _ARTIFACT_FILE_RE.fullmatch(name)) is not None
    }
    if set(actual_digests) != set(expected_artifacts):
        raise BundleError("artifact bytes are missing or unreferenced")
    evidence_records: list[dict[str, object]] = []
    for evidence in evidence_rows:
        evidence_id = str(evidence["evidence_id"])
        index, index_object = indexes[evidence_id]
        artifacts = _validate_index(index, case_id=case_id, evidence_id=evidence_id)
        if not any(item["digest"] == evidence["artifact_digest"] for item in artifacts):
            raise BundleError("Case evidence artifact digest was absent from its index")
        artifact_objects: list[str] = []
        for artifact in artifacts:
            digest = str(artifact["digest"])
            data = _read_stable(
                actual_digests[digest],
                label=f"artifact {digest}",
                max_bytes=max_object_bytes,
            )
            if len(data) != artifact["byte_size"] or _digest(data) != digest:
                raise BundleError(f"artifact {digest} failed digest or size verification")
            artifact_objects.append(
                add_object(
                    name=f"evidence-artifact/{digest.removeprefix('sha256:')}",
                    data=data,
                    media_type=str(artifact["media_type"]),
                    role="evidence_artifact",
                )
            )
        record = dict(evidence)
        record["index_object"] = index_object
        record["artifact_objects"] = artifact_objects
        evidence_records.append(record)

    object_closure = sorted(references)
    lineage = {
        "schema_version": _SCHEMA_VERSION,
        "origin": _ORIGIN,
        "transport_id": transport_id,
        "repository": repository,
        "target_pr": target_pr,
        "case_id": case_id,
        "run_id": run_id,
        "subject_digest": subject_digest,
        "passport_digest": passport_digest,
        "producer_head": producer_head,
        "transport_head": transport_head,
        "transport_ref": transport_ref,
        "case_object": case_object,
        "run_object": run_object,
        "passport_object": passport_object,
        "passport_markdown_object": passport_markdown_object,
        "object_closure": object_closure,
    }
    workbench = {
        "schema_version": _SCHEMA_VERSION,
        "view": "ci_authoritative_case_workbench",
        "origin": _ORIGIN,
        "transport_id": transport_id,
        "repository": repository,
        "target_pr": target_pr,
        "case_id": case_id,
        "run_id": run_id,
        "subject_digest": subject_digest,
        "passport_digest": passport_digest,
        "producer_head": producer_head,
        "transport_head": transport_head,
        "transport_ref": transport_ref,
        "object_closure": object_closure,
    }
    document_without_digest: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "bundle_type": _BUNDLE_TYPE,
        "origin": _ORIGIN,
        "transport_id": transport_id,
        "repository": repository,
        "target_pr": target_pr,
        "case_id": case_id,
        "run_id": run_id,
        "subject_digest": subject_digest,
        "passport_digest": passport_digest,
        "producer_head": producer_head,
        "transport_head": transport_head,
        "transport_ref": transport_ref,
        "case_object": case_object,
        "run_object": run_object,
        "passport_object": passport_object,
        "passport_markdown_object": passport_markdown_object,
        "lineage": lineage,
        "workbench": workbench,
        "evidence": evidence_records,
        "object_closure": object_closure,
        "objects": [objects_by_digest[digest] for digest in object_closure],
    }
    bundle_digest = _digest(_canonical_json_bytes(document_without_digest))
    document = {**document_without_digest, "bundle_digest": bundle_digest}
    bundle_bytes = _canonical_json_bytes(document)
    if len(bundle_bytes) > _MAX_BUNDLE_BYTES:
        raise BundleError("bundle exceeds size limit")
    return (
        document,
        bundle_digest,
        transport_id,
        transport_ref,
        run_id,
        subject_digest,
        passport_digest,
    )


def build_evidence_bundle(
    evidence_root: Path,
    *,
    case_id: str,
    repository: str,
    pr_number: int,
    producer_head: str,
    transport_head: str,
    prefix: str = _DEFAULT_PREFIX,
    max_object_bytes: int = _MAX_OBJECT_BYTES,
) -> BuiltEvidenceBundle:
    """Export one strict local evidence directory into canonical Bundle v1."""

    (
        document,
        bundle_digest,
        transport_id,
        transport_ref,
        run_id,
        subject_digest,
        passport_digest,
    ) = _build_document(
        evidence_root,
        case_id=case_id,
        repository=repository,
        target_pr=pr_number,
        producer_head=producer_head,
        transport_head=transport_head,
        prefix=prefix,
        max_object_bytes=max_object_bytes,
    )
    return BuiltEvidenceBundle(
        bundle_bytes=_canonical_json_bytes(document),
        bundle_digest=bundle_digest,
        transport_id=transport_id,
        transport_ref=transport_ref,
        target_pr=pr_number,
        case_id=case_id,
        run_id=run_id,
        subject_digest=subject_digest,
        passport_digest=passport_digest,
        producer_head=producer_head,
        transport_head=transport_head,
    )


def _verify_document(document: dict[str, Any], *, max_object_bytes: int) -> VerifiedEvidenceBundle:
    required = {
        "schema_version",
        "bundle_type",
        "origin",
        "transport_id",
        "repository",
        "target_pr",
        "case_id",
        "run_id",
        "subject_digest",
        "passport_digest",
        "producer_head",
        "transport_head",
        "transport_ref",
        "case_object",
        "run_object",
        "passport_object",
        "passport_markdown_object",
        "lineage",
        "workbench",
        "evidence",
        "object_closure",
        "objects",
        "bundle_digest",
    }
    if set(document) != required:
        raise BundleError("bundle has missing or extra top-level fields")
    if document["schema_version"] != _SCHEMA_VERSION or document["bundle_type"] != _BUNDLE_TYPE:
        raise BundleError("bundle schema is unsupported")
    if document["origin"] != _ORIGIN:
        raise BundleError("bundle origin is invalid")
    repository = _require_repository(document["repository"])
    target_pr = _require_pr(document["target_pr"])
    case_id = _require_text(document["case_id"], field="case_id")
    run_id = _require_text(document["run_id"], field="run_id")
    subject_digest = _require_digest(document["subject_digest"], field="subject_digest")
    passport_digest = _require_digest(document["passport_digest"], field="passport_digest")
    producer_head = _require_sha1(document["producer_head"], field="producer_head")
    transport_head = _require_sha1(document["transport_head"], field="transport_head")
    transport_ref = _require_text(document["transport_ref"], field="transport_ref")
    if transport_ref != transport_ref_for(
        producer_head=producer_head,
        transport_head=transport_head,
    ):
        raise BundleError("transport_ref is not bound to producer and transport heads")
    transport_id = _require_text(document["transport_id"], field="transport_id")
    if transport_id != _transport_id(
        repository=repository,
        target_pr=target_pr,
        case_id=case_id,
        run_id=run_id,
        subject_digest=subject_digest,
        producer_head=producer_head,
    ):
        raise BundleError("transport_id binding did not match")
    supplied_digest = _require_digest(document["bundle_digest"], field="bundle_digest")
    without_digest = dict(document)
    del without_digest["bundle_digest"]
    if supplied_digest != _digest(_canonical_json_bytes(without_digest)):
        raise BundleError("bundle digest did not match canonical content")

    closure = document["object_closure"]
    objects = document["objects"]
    if not isinstance(closure, list) or not isinstance(objects, list):
        raise BundleError("bundle object closure is invalid")
    if len(objects) > _MAX_OBJECTS or len(closure) != len(objects):
        raise BundleError("bundle object count is invalid")
    if any(type(item) is not str or _SHA256_RE.fullmatch(item) is None for item in closure):
        raise BundleError("bundle object closure contains an invalid digest")
    if closure != sorted(set(closure)):
        raise BundleError("bundle object closure is not unique and sorted")
    object_by_digest: dict[str, Mapping[str, object]] = {}
    object_names: set[str] = set()
    for item in objects:
        if not isinstance(item, Mapping):
            raise BundleError("bundle object is invalid")
        if set(item) != {"digest", "size", "media_type", "role", "name", "data_base64"}:
            raise BundleError("bundle object has missing or extra fields")
        digest = _require_digest(item["digest"], field="object digest")
        if digest in object_by_digest:
            raise BundleError("bundle object digests are not unique")
        if type(item["size"]) is not int or item["size"] < 0 or item["size"] > max_object_bytes:
            raise BundleError("bundle object size is invalid")
        _require_text(item["media_type"], field="object media_type")
        role = _require_text(item["role"], field="object role")
        name = _require_text(item["name"], field="object name")
        if name in object_names:
            raise BundleError("bundle object names are not unique")
        object_names.add(name)
        if role == "case" and name != "case.json":
            raise BundleError("bundle Case object name did not match role")
        if role == "run" and name != "run.json":
            raise BundleError("bundle Run object name did not match role")
        if role == "passport" and name != "passport.json":
            raise BundleError("bundle Passport object name did not match role")
        if role == "passport_markdown" and name != "passport.md":
            raise BundleError("bundle Passport Markdown object name did not match role")
        if role == "evidence_index":
            index_name = PurePosixPath(name)
            if (
                len(index_name.parts) != 2
                or index_name.parts[0] != "evidence-index"
                or _INDEX_FILE_RE.fullmatch(index_name.parts[1]) is None
            ):
                raise BundleError("bundle evidence index object name is invalid")
        if role == "evidence_artifact":
            artifact_name = PurePosixPath(name)
            if (
                len(artifact_name.parts) != 2
                or artifact_name.parts[0] != "evidence-artifact"
                or _ARTIFACT_FILE_RE.fullmatch("sha256_" + artifact_name.parts[1]) is None
                or artifact_name.parts[1] != digest.removeprefix("sha256:")
            ):
                raise BundleError("bundle evidence artifact object name is invalid")
        if role not in {
            "case",
            "run",
            "passport",
            "passport_markdown",
            "evidence_index",
            "evidence_artifact",
        }:
            raise BundleError("bundle object role is unsupported")
        try:
            data = base64.b64decode(item["data_base64"], validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:  # type: ignore[name-defined]
            raise BundleError("bundle object bytes are not valid base64") from exc
        if len(data) != item["size"] or _digest(data) != digest:
            raise BundleError("bundle object digest or size did not match bytes")
        _scan_secret(data, label=str(item["name"]))
        object_by_digest[digest] = item
    if set(object_by_digest) != set(closure):
        raise BundleError("bundle object closure did not match objects")

    def object_data(digest: object, *, label: str) -> bytes:
        digest_text = _require_digest(digest, field=label)
        item = object_by_digest.get(digest_text)
        if item is None:
            raise BundleError(f"{label} is not in object closure")
        return base64.b64decode(item["data_base64"], validate=True)

    for field, role, name in (
        ("case_object", "case", "case.json"),
        ("run_object", "run", "run.json"),
        ("passport_object", "passport", "passport.json"),
        ("passport_markdown_object", "passport_markdown", "passport.md"),
    ):
        digest = _require_digest(document[field], field=field)
        item = object_by_digest.get(digest)
        if item is None or item.get("role") != role or item.get("name") != name:
            raise BundleError(f"bundle {field} did not reference the expected object")

    case = _parse_json(object_data(document["case_object"], label="case_object"), label="bundle Case")
    run = _parse_json(object_data(document["run_object"], label="run_object"), label="bundle Run")
    passport = _parse_json(
        object_data(document["passport_object"], label="passport_object"),
        label="bundle Passport",
    )
    passport_markdown = object_data(
        document["passport_markdown_object"], label="passport_markdown_object"
    )
    if _digest(_canonical_json_bytes(case)) != document["case_object"]:
        raise BundleError("bundle Case object is not canonical")
    if _digest(_canonical_json_bytes(run)) != document["run_object"]:
        raise BundleError("bundle Run object is not canonical")
    if _digest(_canonical_json_bytes(passport)) != document["passport_object"]:
        raise BundleError("bundle Passport object is not canonical")
    if (
        case.get("case_id") != case_id
        or case.get("subject_digest") != subject_digest
        or passport.get("case_id") != case_id
        or passport.get("subject_digest") != subject_digest
        or _stable_passport_digest(passport) != passport_digest
    ):
        raise BundleError("bundle core object bindings did not match")
    parsed_run_id, _, evidence_rows = _validate_core_payloads(
        case=case,
        run=run,
        passport=passport,
        case_id=case_id,
    )
    if parsed_run_id != run_id:
        raise BundleError("bundle run_id did not match Run")

    expected_references = {
        document["case_object"],
        document["run_object"],
        document["passport_object"],
        document["passport_markdown_object"],
    }
    evidence = document["evidence"]
    if not isinstance(evidence, list) or len(evidence) != len(evidence_rows):
        raise BundleError("bundle evidence closure is invalid")
    evidence_by_id: dict[str, Mapping[str, object]] = {}
    for item in evidence:
        if not isinstance(item, Mapping):
            raise BundleError("bundle evidence row is invalid")
        evidence_id = _require_text(item.get("evidence_id"), field="evidence_id")
        if evidence_id in evidence_by_id:
            raise BundleError("bundle evidence IDs are not unique")
        evidence_by_id[evidence_id] = item
        index_digest = _require_digest(item.get("index_object"), field="index_object")
        index_object = object_by_digest.get(index_digest)
        if index_object is None or index_object.get("role") != "evidence_index":
            raise BundleError("bundle index_object did not reference an evidence index")
        expected_references.add(index_digest)
        artifact_objects = item.get("artifact_objects")
        if not isinstance(artifact_objects, list) or not artifact_objects:
            raise BundleError("bundle evidence artifact closure is invalid")
        for digest in artifact_objects:
            digest_text = _require_digest(digest, field="artifact_object")
            artifact_object = object_by_digest.get(digest_text)
            if artifact_object is None or artifact_object.get("role") != "evidence_artifact":
                raise BundleError("bundle artifact_object did not reference an evidence artifact")
            expected_references.add(digest_text)
    if {str(item.get("evidence_id")) for item in evidence_rows} != set(evidence_by_id):
        raise BundleError("bundle evidence IDs did not match Case")

    for evidence_row in evidence_rows:
        evidence_id = str(evidence_row["evidence_id"])
        item = evidence_by_id[evidence_id]
        if item.get("artifact_digest") != evidence_row.get("artifact_digest"):
            raise BundleError("bundle evidence digest did not match Case")
        index = _parse_json(object_data(item["index_object"], label="index_object"), label="bundle Evidence index")
        artifacts = _validate_index(index, case_id=case_id, evidence_id=evidence_id)
        artifact_objects = item["artifact_objects"]
        assert isinstance(artifact_objects, list)
        if len(artifacts) != len(artifact_objects):
            raise BundleError("bundle evidence artifact count did not match index")
        for artifact, artifact_object in zip(artifacts, artifact_objects):
            data = object_data(artifact_object, label="artifact_object")
            digest = str(artifact["digest"])
            if len(data) != artifact["byte_size"] or _digest(data) != digest:
                raise BundleError("bundle evidence artifact did not match index")

    if expected_references != set(closure):
        raise BundleError("bundle contains an unreferenced or missing object")
    expected_transport_ref = transport_ref_for(
        producer_head=producer_head,
        transport_head=transport_head,
    )
    lineage = document["lineage"]
    workbench = document["workbench"]
    if not isinstance(lineage, Mapping) or not isinstance(workbench, Mapping):
        raise BundleError("bundle projections are invalid")
    for projection, required_projection in ((lineage, "lineage"), (workbench, "workbench")):
        for field, expected in {
            "schema_version": _SCHEMA_VERSION,
            "origin": _ORIGIN,
            "transport_id": transport_id,
            "repository": repository,
            "target_pr": target_pr,
            "case_id": case_id,
            "run_id": run_id,
            "subject_digest": subject_digest,
            "passport_digest": passport_digest,
            "producer_head": producer_head,
            "transport_head": transport_head,
            "transport_ref": expected_transport_ref,
            "object_closure": closure,
        }.items():
            if projection.get(field) != expected:
                raise BundleError(f"bundle {required_projection} binding did not match")
    if lineage.get("case_object") != document["case_object"] or lineage.get("run_object") != document["run_object"]:
        raise BundleError("bundle lineage object references did not match")
    if lineage.get("passport_object") != document["passport_object"] or lineage.get("passport_markdown_object") != document["passport_markdown_object"]:
        raise BundleError("bundle lineage Passport references did not match")
    return VerifiedEvidenceBundle(document=document, bundle_digest=supplied_digest)


def verify_evidence_bundle(
    bundle_bytes: bytes,
    *,
    max_object_bytes: int = _MAX_OBJECT_BYTES,
    max_bundle_bytes: int = _MAX_BUNDLE_BYTES,
) -> VerifiedEvidenceBundle:
    """Verify canonical bytes, content closure, cross-bindings, and secrets."""

    if type(bundle_bytes) is not bytes:
        raise TypeError("bundle_bytes must be bytes")
    if len(bundle_bytes) > max_bundle_bytes:
        raise BundleError("bundle exceeds size limit")
    _scan_secret(bundle_bytes, label="bundle")
    document = _parse_json(bundle_bytes, label="bundle")
    if bundle_bytes != _canonical_json_bytes(document):
        raise BundleError("bundle is not canonical JSON")
    return _verify_document(document, max_object_bytes=max_object_bytes)


__all__ = [
    "BundleError",
    "BuiltEvidenceBundle",
    "VerifiedEvidenceBundle",
    "build_evidence_bundle",
    "canonical_json_bytes",
    "verify_evidence_bundle",
]
