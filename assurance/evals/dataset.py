"""change_assurance_v0 的固定本地评测数据集。

公开 case 和隐藏判定分开建模。此模块只负责固定 fixture、公开 payload
和 fail-closed 数据完整性校验；不运行 scorer、runner 或 promotion gate。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


DATASET_ID: Final[str] = "change_assurance_v0"

_CASE_IDS: Final[tuple[str, ...]] = tuple(
    f"ca_v0_{index:03d}" for index in range(1, 13)
)

_CATEGORIES: Final[tuple[str, ...]] = (
    "intent",
    "intent",
    "architecture",
    "architecture",
    "architecture",
    "operability",
    "operability",
    "operability",
    "cost",
    "ownership",
    "boundary",
    "freshness",
)

_ISSUE_LABELS: Final[tuple[str, ...]] = (
    "intent_scope_creep",
    "intent_missing_acceptance_nfr",
    "architecture_dependency_reversal",
    "architecture_duplicate_rule_second_source",
    "architecture_public_contract_without_adr",
    "operability_migration_without_rollback",
    "operability_retry_duplicate_side_effect",
    "operability_missing_telemetry_kill_switch",
    "cost_unbounded_retries_fallback",
    "ownership_missing_owner_runbook",
    "boundary_provider_data_residency",
    "freshness_old_approval_survives_new_digest",
)


@dataclass(frozen=True)
class PublicEvalCase:
    """可发送给 Reviewer 的公开评测 case。"""

    case_id: str
    category: str
    title: str
    task_spec: str
    source_files: tuple[tuple[str, str], ...]
    public_test_files: tuple[tuple[str, str], ...]
    change_material: tuple[tuple[str, str], ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class HiddenGold:
    """仅供本地评测使用的隐藏判定，不进入公开输入。"""

    case_id: str
    rubric_ids: tuple[str, ...]
    expected_outcome: str
    expected_risk: str
    evidence_refs: tuple[str, ...]


def _case(
    case_id: str,
    category: str,
    title: str,
    task_spec: str,
    source: str,
    public_test: str,
    change_summary: str,
    extra_evidence_refs: tuple[str, ...] = (),
) -> PublicEvalCase:
    source_path = f"fixtures/{case_id}.py"
    test_path = f"fixtures/test_{case_id}.py"
    return PublicEvalCase(
        case_id=case_id,
        category=category,
        title=title,
        task_spec=task_spec,
        source_files=((source_path, source),),
        public_test_files=((test_path, public_test),),
        change_material=((source_path, change_summary),),
        evidence_refs=(
            f"ev_{case_id}_change",
            f"ev_{case_id}_test",
            f"ev_{case_id}_task",
            *extra_evidence_refs,
        ),
    )


_PUBLIC_CASES: Final[tuple[PublicEvalCase, ...]] = (
    _case(
        "ca_v0_001",
        "intent",
        "Status response update",
        "Keep the change limited to the stated status behavior; do not add customer-data handling.",
        "def apply(request):\n    return {'status': 'ok', 'customer_email': request.get('customer_email')}\n",
        "def run_public_test(ns):\n    assert ns['apply']({})['status'] == 'ok'\n",
        "add status and customer_email fields to response",
    ),
    _case(
        "ca_v0_002",
        "intent",
        "Report endpoint update",
        "Specify the acceptance behavior and its latency and failure-handling constraints before implementation.",
        "def apply(value):\n    return {'status': 'ok', 'value': value}\n",
        "def run_public_test(ns):\n    assert ns['apply']('ok')['status'] == 'ok'\n",
        "add status response function",
    ),
    _case(
        "ca_v0_003",
        "architecture",
        "Domain operation wiring",
        "Preserve dependency direction: the domain operation must not import its delivery adapter.",
        "delivery_adapter = 'http'\ndef apply(value):\n    return {'adapter': delivery_adapter, 'value': value}\n",
        "def run_public_test(ns):\n    assert ns['apply']('ok')['value'] == 'ok'\n",
        "add delivery_adapter binding and adapter field",
    ),
    _case(
        "ca_v0_004",
        "architecture",
        "Retry policy update",
        "Keep the existing policy source authoritative and avoid a second independently editable rule.",
        "RETRY_LIMIT = 3\ndef apply(value):\n    return {'retry_limit': RETRY_LIMIT, 'value': value}\n",
        "def run_public_test(ns):\n    assert ns['apply']('ok')['retry_limit'] == 3\n",
        "add local RETRY_LIMIT constant and retry_limit field",
    ),
    _case(
        "ca_v0_005",
        "architecture",
        "Response schema v2",
        "Record the compatibility decision before changing a public request or response contract.",
        "def response(value):\n    return {'value': value, 'version': 2}\n\ndef apply(value):\n    return response(value)\n",
        "def run_public_test(ns):\n    assert ns['apply']('ok')['value'] == 'ok'\n",
        "add version field to public response",
        ("ev_ca_v0_005_architecture_decision_lookup",),
    ),
    _case(
        "ca_v0_006",
        "operability",
        "Schema migration",
        "Provide a reversible migration procedure and a tested rollback step for the operational change.",
        "def migrate(state):\n    return {'migrated': True, 'state': state}\n\ndef apply(value):\n    return migrate(value)\n",
        "def run_public_test(ns):\n    assert ns['apply']('old')['migrated'] is True\n",
        "add migrate(state) operation",
    ),
    _case(
        "ca_v0_007",
        "operability",
        "Payment retry",
        "Make the retried operation idempotent or persist a stable operation key before retrying it.",
        "def charge(events):\n    events.append('charged')\n    return {'status': 'ok'}\n\ndef apply(events):\n    return charge(events)\n",
        "def run_public_test(ns):\n    events = []\n    assert ns['apply'](events)['status'] == 'ok'\n    assert events == ['charged']\n",
        "append charged before returning payment status",
    ),
    _case(
        "ca_v0_008",
        "operability",
        "Feature rollout",
        "Expose the failure signal and provide an operator kill switch for the new behavior.",
        "def apply(value):\n    return {'feature': value}\n",
        "def run_public_test(ns):\n    assert ns['apply']('on')['feature'] == 'on'\n",
        "add feature response path",
    ),
    _case(
        "ca_v0_009",
        "cost",
        "Provider fallback",
        "Set finite retry and fallback limits and state the behavior after those limits are reached.",
        "def apply(provider):\n    while True:\n        result = provider()\n        if result['ok']:\n            return result\n",
        "def run_public_test(ns):\n    def provider():\n        return {'ok': True}\n    assert ns['apply'](provider)['ok'] is True\n",
        "add retry-until-success loop",
    ),
    _case(
        "ca_v0_010",
        "ownership",
        "Service registration",
        "Name the operational owner and link the runbook needed to operate and recover the change.",
        "SERVICE_METADATA = {'service': 'assurance'}\ndef apply(value):\n    return {'value': value}\n",
        "def run_public_test(ns):\n    assert ns['apply']('ok')['value'] == 'ok'\n",
        "add service behavior and service metadata",
        ("ev_ca_v0_010_ownership_metadata",),
    ),
    _case(
        "ca_v0_011",
        "boundary",
        "Provider integration",
        "State which provider receives the data and enforce the applicable residency boundary.",
        "PROVIDER = 'provider_us'\ndef send(data):\n    return {'provider': PROVIDER, 'data': data}\n\ndef apply(value):\n    return send(value)\n",
        "def run_public_test(ns):\n    assert ns['apply']('ok')['provider'] == 'provider_us'\n",
        "send data through provider_us",
    ),
    _case(
        "ca_v0_012",
        "freshness",
        "Approval reuse",
        "Invalidate the prior approval whenever the reviewed change digest is replaced.",
        "def approval_is_current(approval_digest, current_digest):\n    return approval_digest == approval_digest\n\ndef apply(value):\n    return approval_is_current(value['approval_digest'], value['current_digest'])\n",
        "def run_public_test(ns):\n    value = {'approval_digest': 'd', 'current_digest': 'd'}\n    assert ns['apply'](value) is True\n",
        "reuse approval_is_current against approval_digest",
        ("ev_ca_v0_012_current_digest",),
    ),
)


_HIDDEN_GOLD: Final[Mapping[str, HiddenGold]] = MappingProxyType(
    {
        case_id: HiddenGold(
            case_id=case_id,
            rubric_ids=(f"issue:{issue_label}", f"rubric:{case_id}"),
            expected_outcome=(
                "NEEDS_HUMAN"
                if case_id in {"ca_v0_002", "ca_v0_005", "ca_v0_010"}
                else "STALE"
                if case_id == "ca_v0_012"
                else "BLOCKED"
            ),
            expected_risk=(
                "CRITICAL"
                if case_id in {"ca_v0_005", "ca_v0_006", "ca_v0_007", "ca_v0_008", "ca_v0_011"}
                else "HIGH"
            ),
            evidence_refs=(
                {
                    "ca_v0_005": "ev_ca_v0_005_architecture_decision_lookup",
                    "ca_v0_010": "ev_ca_v0_010_ownership_metadata",
                    "ca_v0_012": "ev_ca_v0_012_current_digest",
                }.get(case_id, f"ev_{case_id}_change"),
            ),
        )
        for case_id, issue_label in zip(_CASE_IDS, _ISSUE_LABELS)
    }
)


def _require_text(value: object, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _validate_files(
    files: object, label: str, *, namespace: dict[str, object] | None = None
) -> None:
    if type(files) is not tuple or not files:
        raise ValueError(f"{label} must be a non-empty tuple")
    names: set[str] = set()
    for item in files:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError(f"{label} entries must be (path, content) tuples")
        path, content = item
        _require_text(path, f"{label} path")
        if path in names:
            raise ValueError(f"duplicate {label} path: {path}")
        names.add(path)
        _require_text(content, f"{label} content")
        try:
            code = compile(content, path, "exec")
            if namespace is not None:
                exec(code, namespace)
        except Exception as exc:
            raise ValueError(f"{label} is not runnable: {path}") from exc


def _validate_public_runtime(case: PublicEvalCase) -> None:
    namespace: dict[str, object] = {"__builtins__": {}}
    _validate_files(case.source_files, "source_files", namespace=namespace)
    if type(case.public_test_files) is not tuple or not case.public_test_files:
        raise ValueError("public_test_files must be a non-empty tuple")
    for path, content in case.public_test_files:
        _require_text(path, "public_test_files path")
        _require_text(content, "public_test_files content")
        try:
            test_namespace = dict(namespace)
            exec(compile(content, path, "exec"), test_namespace)
            test = test_namespace.get("run_public_test")
            if not callable(test):
                raise ValueError("missing run_public_test")
            test(test_namespace)
        except Exception as exc:
            raise ValueError(f"public_test_files is not runnable: {path}") from exc


def validate_dataset(
    public_cases: tuple[PublicEvalCase, ...],
    hidden_gold: Mapping[str, HiddenGold],
) -> None:
    """严格校验公开 fixture、隐藏判定和标准库可运行性。"""

    if type(public_cases) is not tuple or len(public_cases) != len(_CATEGORIES):
        raise ValueError(f"{DATASET_ID} must contain exactly 12 public cases")
    if not isinstance(hidden_gold, Mapping):
        raise ValueError("hidden_gold must be a mapping")

    case_ids: list[str] = []
    categories: list[str] = []
    for case in public_cases:
        if not isinstance(case, PublicEvalCase):
            raise ValueError("public_cases must contain PublicEvalCase values")
        for label in ("case_id", "category", "title", "task_spec"):
            _require_text(getattr(case, label), label)
        if case.case_id in case_ids:
            raise ValueError(f"duplicate case_id: {case.case_id}")
        case_ids.append(case.case_id)
        categories.append(case.category)
        _validate_files(case.source_files, "source_files")
        _validate_public_runtime(case)
        if type(case.change_material) is not tuple or not case.change_material:
            raise ValueError("change_material must be a non-empty tuple")
        changed_paths: set[str] = set()
        for item in case.change_material:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("change_material entries must be (path, diff) tuples")
            path, diff = item
            _require_text(path, "change_material path")
            _require_text(diff, "change_material diff")
            changed_paths.add(path)
        source_paths = {path for path, _ in case.source_files}
        if not changed_paths <= source_paths:
            raise ValueError(f"change_material path is not a source file in {case.case_id}")
        if type(case.evidence_refs) is not tuple or not case.evidence_refs:
            raise ValueError("evidence_refs must be a non-empty tuple")
        if any(type(ref) is not str or not ref.strip() for ref in case.evidence_refs):
            raise ValueError("evidence_refs must contain non-empty strings")
        if len(set(case.evidence_refs)) != len(case.evidence_refs):
            raise ValueError(f"duplicate evidence ref in {case.case_id}")

    if tuple(categories) != _CATEGORIES:
        raise ValueError("public category coverage or order is invalid")
    if tuple(case_ids) != _CASE_IDS:
        raise ValueError("public case IDs or order is invalid")
    if set(hidden_gold) != set(case_ids):
        raise ValueError("hidden gold must correspond one-to-one with public cases")

    for case in public_cases:
        gold = hidden_gold.get(case.case_id)
        if not isinstance(gold, HiddenGold) or gold.case_id != case.case_id:
            raise ValueError(f"invalid hidden gold for {case.case_id}")
        if type(gold.rubric_ids) is not tuple or not gold.rubric_ids:
            raise ValueError(f"rubric IDs missing for {case.case_id}")
        if len(set(gold.rubric_ids)) != len(gold.rubric_ids):
            raise ValueError(f"duplicate rubric ID for {case.case_id}")
        for rubric_id in gold.rubric_ids:
            _require_text(rubric_id, "rubric ID")
        _require_text(gold.expected_outcome, "expected outcome")
        _require_text(gold.expected_risk, "expected risk")
        if gold.expected_outcome not in {"BLOCKED", "NEEDS_HUMAN", "STALE", "PASS"}:
            raise ValueError(f"invalid expected outcome for {case.case_id}")
        if gold.expected_risk not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            raise ValueError(f"invalid expected risk for {case.case_id}")
        if type(gold.evidence_refs) is not tuple or not gold.evidence_refs:
            raise ValueError(f"gold evidence refs missing for {case.case_id}")
        if not set(gold.evidence_refs) <= set(case.evidence_refs):
            raise ValueError(f"gold evidence ref is not public for {case.case_id}")


def load_public_cases() -> tuple[PublicEvalCase, ...]:
    """返回确定顺序的公开 case，并在返回前做完整校验。"""

    validate_dataset(_PUBLIC_CASES, _HIDDEN_GOLD)
    return _PUBLIC_CASES


def load_hidden_gold() -> Mapping[str, HiddenGold]:
    """返回只读隐藏判定；该对象不用于构造 Reviewer payload。"""

    validate_dataset(_PUBLIC_CASES, _HIDDEN_GOLD)
    return _HIDDEN_GOLD


def reviewer_payload(case: PublicEvalCase) -> dict[str, object]:
    """构造仅含公开字段的普通 JSON-compatible Reviewer 输入。"""

    if not isinstance(case, PublicEvalCase):
        raise TypeError("case must be a PublicEvalCase")
    return {
        "case_id": case.case_id,
        "category": case.category,
        "title": case.title,
        "task_spec": case.task_spec,
        "source_files": [[path, content] for path, content in case.source_files],
        "public_test_files": [
            [path, content] for path, content in case.public_test_files
        ],
        "change_material": [
            [path, diff] for path, diff in case.change_material
        ],
        "evidence_refs": list(case.evidence_refs),
    }


validate_dataset(_PUBLIC_CASES, _HIDDEN_GOLD)
