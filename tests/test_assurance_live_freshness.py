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


def test_v2_live_freshness_allows_ignored_count_only_change(tmp_path):
    service, intent = _service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="freshness-count-only"))
    binding = result.bundle.freshness_source_binding

    class _CountOnlyCollector:
        def __init__(self, **kwargs):
            from assurance.snapshot import GitSnapshotCollector

            self._collector = GitSnapshotCollector(**kwargs)

        def __getattr__(self, name):
            return getattr(self._collector, name)

        def collect(self, *args, **kwargs):
            collected = self._collector.collect(*args, **kwargs)
            snapshot = collected.snapshot.model_copy(
                update={
                    "ignored_files_lower_bound": (
                        collected.snapshot.ignored_files_lower_bound + 1
                    )
                }
            )
            return collected.model_copy(update={"snapshot": snapshot})

    checker = LiveFreshnessChecker(
        workspace_root=tmp_path,
        git_collector_factory=_CountOnlyCollector,
        clock=lambda: datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )

    fresh = checker.check(binding, result.bundle.git.snapshot, result.bundle.intake.snapshot)

    assert fresh.status is FreshnessStatus.FRESH
