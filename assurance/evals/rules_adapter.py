"""Deterministic public-only Rules Only evaluation adapter."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from typing import Final

from .adapters import derive_finding_id
from .runner import ArmRunResult, EvalFinding


RULE_VERSION: Final[str] = "rules-public-v1"
_ARM: Final[str] = "rules_only"
_ROLE: Final[str] = "rules"
_MODEL_REF: Final[str] = "rules:public-v0"
_ERROR_CODE: Final[str] = "invalid_public_payload"
_PUBLIC_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "case_id",
        "category",
        "title",
        "task_spec",
        "source_files",
        "public_test_files",
        "change_material",
        "evidence_refs",
    }
)
_RULE_CODES: Final[dict[str, str]] = {
    "bounded_attempts": "rule:bounded_attempts",
    "data_scope": "rule:data_scope",
    "independent_digest_comparison": "rule:independent_digest_comparison",
    "migration_reversibility": "rule:migration_reversibility",
    "operator_control": "rule:operator_control",
    "ownership_metadata": "rule:ownership_metadata",
}
_REVERSE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:rollback|revert|downgrade|reverse)"
)
_CUSTOMER_FIELD_RE: Final[re.Pattern[str]] = re.compile(
    r"\bcustomer(?:[_-][a-z0-9]+|\s+[a-z0-9]+)?\b"
)
_OPERATOR_CONTROL_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:kill[_ -]?switch|disable(?:d|r)?|feature[_ -]?(?:enabled|disabled|on|off))\b"
)


def _require_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} is invalid")
    return value


def _validate_json_value(value: object, label: str = "payload") -> None:
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} is not finite")
        return
    if type(value) is str:
        if "\x00" in value:
            raise ValueError(f"{label} contains NUL")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_value(item, f"{label}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or "\x00" in key:
                raise ValueError(f"{label} has an invalid key")
            _validate_json_value(item, f"{label}.{key}")
        return
    raise ValueError(f"{label} is not JSON-compatible")


def _validate_refs(value: object) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise ValueError("evidence_refs is invalid")
    refs = tuple(_require_text(item, "evidence_ref") for item in value)
    if len(set(refs)) != len(refs):
        raise ValueError("evidence_refs contains duplicates")
    return refs


def _validate_files(value: object, label: str) -> tuple[tuple[str, str], ...]:
    if type(value) is not list or not value:
        raise ValueError(f"{label} is invalid")
    result: list[tuple[str, str]] = []
    paths: set[str] = set()
    for item in value:
        if type(item) is not list or len(item) != 2:
            raise ValueError(f"{label} entry is invalid")
        path = _require_text(item[0], f"{label} path")
        content = _require_text(item[1], f"{label} content")
        if path in paths:
            raise ValueError(f"{label} contains duplicate paths")
        paths.add(path)
        result.append((path, content))
    return tuple(result)


def _validate_change_material(
    value: object, source_paths: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    material = _validate_files(value, "change_material")
    if not {path for path, _ in material} <= source_paths:
        raise ValueError("change_material path is not a source path")
    return material


def _validate_payload(payload: object) -> tuple[
    str,
    str,
    str,
    str,
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    tuple[str, ...],
]:
    if type(payload) is not dict:
        raise ValueError("public payload must be a dict")
    _validate_json_value(payload)
    if set(payload) != _PUBLIC_FIELDS:
        raise ValueError("public payload fields are invalid")
    case_id = _require_text(payload["case_id"], "case_id")
    category = _require_text(payload["category"], "category")
    title = _require_text(payload["title"], "title")
    task_spec = _require_text(payload["task_spec"], "task_spec")
    source_files = _validate_files(payload["source_files"], "source_files")
    public_test_files = _validate_files(
        payload["public_test_files"], "public_test_files"
    )
    change_material = _validate_change_material(
        payload["change_material"], frozenset(path for path, _ in source_files)
    )
    evidence_refs = _validate_refs(payload["evidence_refs"])
    return (
        case_id,
        category,
        title,
        task_spec,
        source_files,
        public_test_files,
        change_material,
        evidence_refs,
    )


def _parse_files(files: tuple[tuple[str, str], ...]) -> tuple[ast.AST, ...]:
    trees: list[ast.AST] = []
    for path, content in files:
        try:
            trees.append(ast.parse(content, filename=path, mode="exec"))
        except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
            raise ValueError("public source is not parseable") from exc
    return tuple(trees)


def _source_text(files: tuple[tuple[str, str], ...]) -> str:
    return "\n".join(content for _, content in files)


def _function_names(trees: tuple[ast.AST, ...]) -> tuple[str, ...]:
    names: list[str] = []
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name.lower())
            elif isinstance(node, ast.Name):
                names.append(node.id.lower())
    return tuple(names)


def _has_self_comparison(trees: tuple[ast.AST, ...]) -> bool:
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = ast.dump(node.left, annotate_fields=True)
            if any(left == ast.dump(comparator, annotate_fields=True) for comparator in node.comparators):
                return True
    return False


def _has_unbounded_loop(trees: tuple[ast.AST, ...]) -> bool:
    return any(
        isinstance(node, ast.While)
        and isinstance(node.test, ast.Constant)
        and node.test.value is True
        for tree in trees
        for node in ast.walk(tree)
    )


def _has_migration_without_reverse(trees: tuple[ast.AST, ...]) -> bool:
    names = _function_names(trees)
    has_migration = any("migrat" in name for name in names)
    has_reverse = any(_REVERSE_NAME_RE.search(name) for name in names)
    return has_migration and not has_reverse


def _has_operator_control(source_text: str) -> bool:
    return bool(_OPERATOR_CONTROL_RE.search(source_text.lower()))


def _requires_customer_exclusion(task_spec: str) -> bool:
    task = task_spec.lower()
    return "customer" in task and any(
        marker in task
        for marker in ("do not", "don't", "without", "exclude", "not add")
    )


def _requires_operator_control(task_spec: str) -> bool:
    return bool(re.search(r"kill\s*[-_ ]?\s*switch", task_spec.lower()))


def _add_question(questions: list[str], text: str) -> None:
    if text not in questions:
        questions.append(text)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _receipt_ref(
    payload: dict[str, object],
    findings: tuple[EvalFinding, ...],
    questions: tuple[str, ...],
    outcome: str,
) -> str:
    material = {
        "payload": payload,
        "rule_version": RULE_VERSION,
        "findings": [
            {
                "finding_id": finding.finding_id,
                "claim": finding.claim,
                "severity": finding.severity,
                "blocking": finding.blocking,
                "evidence_refs": list(finding.evidence_refs),
                "predicted_issue_ids": list(finding.predicted_issue_ids),
                "reviewer_role": finding.reviewer_role,
            }
            for finding in findings
        ],
        "questions": list(questions),
        "predicted_outcome": outcome,
    }
    return "sha256:" + hashlib.sha256(_canonical_json(material)).hexdigest()


def _safe_case_id(payload: object) -> str:
    if type(payload) is dict:
        candidate = payload.get("case_id")
        if type(candidate) is str and candidate.strip() and "\x00" not in candidate:
            return candidate
    return "invalid_case"


class RulesOnlyAdapter:
    """运行不读取隐藏判定的通用 public-only 规则基线。"""

    @staticmethod
    def _schema_invalid(case_id: str) -> ArmRunResult:
        return ArmRunResult(
            case_id=case_id,
            arm=_ARM,
            status="schema_invalid",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0,
            error_code=_ERROR_CODE,
            executed_roles=(_ROLE,),
            model_refs=(_MODEL_REF,),
        )

    @staticmethod
    def run(payload: object) -> ArmRunResult:
        case_id = _safe_case_id(payload)
        try:
            (
                case_id,
                _category,
                _title,
                task_spec,
                source_files,
                public_test_files,
                _change_material,
                evidence_refs,
            ) = _validate_payload(payload)
            source_trees = _parse_files(source_files)
            _parse_files(public_test_files)
            source_text = _source_text(source_files)
            task_lower = task_spec.lower()
            change_ref = next(
                (ref for ref in evidence_refs if ref.endswith("_change")),
                evidence_refs[0],
            )
            findings: list[EvalFinding] = []
            questions: list[str] = []
            seen_rules: set[str] = set()

            def add_finding(
                rule_key: str,
                claim: str,
                severity: str,
                blocking: bool,
            ) -> None:
                rule_code = _RULE_CODES[rule_key]
                if rule_code in seen_rules:
                    return
                seen_rules.add(rule_code)
                refs = (change_ref,)
                issue_ids = (rule_code,)
                findings.append(
                    EvalFinding(
                        finding_id=derive_finding_id(
                            case_id,
                            _ARM,
                            _ROLE,
                            claim,
                            refs,
                            issue_ids,
                        ),
                        claim=claim,
                        severity=severity,
                        blocking=blocking,
                        evidence_refs=refs,
                        predicted_issue_ids=issue_ids,
                        reviewer_role=_ROLE,
                    )
                )

            if _has_self_comparison(source_trees):
                add_finding(
                    "independent_digest_comparison",
                    "A comparison reuses the same expression on both sides.",
                    "high",
                    False,
                )
            if _has_unbounded_loop(source_trees):
                add_finding(
                    "bounded_attempts",
                    "A retry-like loop has no visible finite termination condition.",
                    "high",
                    True,
                )
            if _has_migration_without_reverse(source_trees):
                add_finding(
                    "migration_reversibility",
                    "A migration operation has no visible reverse operation.",
                    "high",
                    True,
                )
            if _requires_operator_control(task_spec) and not _has_operator_control(
                source_text
            ):
                add_finding(
                    "operator_control",
                    "The task requests an operator control that is not visible in source.",
                    "high",
                    True,
                )
            if _requires_customer_exclusion(task_spec) and _CUSTOMER_FIELD_RE.search(
                source_text.lower()
            ):
                add_finding(
                    "data_scope",
                    "Source exposes customer data while the public task excludes it.",
                    "high",
                    True,
                )
            source_lower = source_text.lower()
            if (
                "owner" in task_lower
                and "runbook" in task_lower
                and "service" in source_lower
                and "owner" not in source_lower
                and "runbook" not in source_lower
            ):
                add_finding(
                    "ownership_metadata",
                    "Service metadata does not visibly include owner and runbook links.",
                    "high",
                    True,
                )

            if "acceptance" in task_lower and (
                "latency" in task_lower or "failure" in task_lower
            ):
                _add_question(
                    questions,
                    "Please confirm measurable acceptance and non-functional limits.",
                )
            if "dependency direction" in task_lower or "authoritative" in task_lower:
                _add_question(
                    questions,
                    "Please confirm the authoritative dependency or policy source.",
                )
            if "compatibility decision" in task_lower:
                _add_question(
                    questions,
                    "Please provide the public compatibility decision reference.",
                )
            if "idempotent" in task_lower or "stable operation key" in task_lower:
                _add_question(
                    questions,
                    "Please confirm the retry idempotency mechanism.",
                )
            if "residency" in task_lower:
                _add_question(
                    questions,
                    "Please confirm the provider residency enforcement evidence.",
                )

            if any(
                finding.predicted_issue_ids
                == (_RULE_CODES["independent_digest_comparison"],)
                for finding in findings
            ):
                outcome = "STALE"
            elif any(finding.blocking for finding in findings):
                outcome = "BLOCKED"
            elif questions:
                outcome = "NEEDS_HUMAN"
            else:
                outcome = "PASS"
            finding_tuple = tuple(findings)
            question_tuple = tuple(questions)
            return ArmRunResult(
                case_id=case_id,
                arm=_ARM,
                status="success",
                findings=finding_tuple,
                questions=question_tuple,
                predicted_outcome=outcome,
                receipt_ref=_receipt_ref(
                    payload, finding_tuple, question_tuple, outcome
                ),
                input_tokens=0,
                output_tokens=0,
                cost_usd=0,
                latency_seconds=None,
                executed_roles=(_ROLE,),
                model_refs=(_MODEL_REF,),
            )
        except Exception:
            return RulesOnlyAdapter._schema_invalid(case_id)


def run_rules_only(payload: object) -> ArmRunResult:
    """便捷运行入口，等价于 ``RulesOnlyAdapter.run(payload)``。"""

    return RulesOnlyAdapter.run(payload)


__all__ = ["RULE_VERSION", "RulesOnlyAdapter", "run_rules_only"]
