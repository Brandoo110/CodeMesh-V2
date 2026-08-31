"""Focused subject-identity freshness tests."""

import asyncio
from datetime import datetime, timezone

from assurance.live_freshness import FreshnessStatus, LiveFreshnessChecker

from tests.test_assurance_run_service import _service


def test_v2_binding_same_scope_is_fresh_and_scope_drift_fails_closed(tmp_path):
    service, intent = _service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="freshness-v2"))
    binding = result.bundle.freshness_source_binding
    assert binding.subject_identity_version == "v2"

    checker = LiveFreshnessChecker(
        workspace_root=tmp_path,
        clock=lambda: datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )
    fresh = checker.check(binding, result.bundle.git.snapshot, result.bundle.intake.snapshot)
    assert fresh.status is FreshnessStatus.FRESH

    drifted = binding.model_copy(update={"policy_paths": ()})
    stale = checker.check(drifted, result.bundle.git.snapshot, result.bundle.intake.snapshot)
    assert stale.status is FreshnessStatus.STALE
