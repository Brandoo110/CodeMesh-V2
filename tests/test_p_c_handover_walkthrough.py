import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from assurance.artifacts import ArtifactStore
from assurance.contracts import Evidence
from assurance.snapshot import GitSnapshot
from scripts.p_c_handover_walkthrough import _PCHandoverContextBuilder


SUBJECT_DIGEST = "sha256:" + "a" * 64
ARTIFACT_DIGEST = "sha256:" + "b" * 64
FIXED_TIME = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


class TestPCHandoverContextBuilder(unittest.TestCase):
    def test_accepts_typed_git_snapshot_keyword(self):
        evidence = Evidence(
            evidence_id="ev-git-snapshot",
            subject_digest=SUBJECT_DIGEST,
            kind="git_snapshot",
            producer="collector.git",
            artifact_digest=ARTIFACT_DIGEST,
            source_ref="git_snapshot:example/p-c-fresh:base:head:base_to_worktree",
            status="success",
            trust_level="deterministic",
            collected_at=FIXED_TIME,
        )
        git_snapshot = GitSnapshot(
            subject_digest=SUBJECT_DIGEST,
            repository="example/p-c-fresh",
            base_revision="1" * 40,
            head_revision="2" * 40,
            worktree_dirty=False,
            changed_files_total=0,
            diff_artifact_digest=ARTIFACT_DIGEST,
            diff_bytes=0,
            diff_truncated=False,
            files_truncated=False,
            ignored_files_lower_bound=0,
            ignored_scan_truncated=False,
            complete=True,
            collected_at=FIXED_TIME,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            plan = _PCHandoverContextBuilder().prepare(
                (evidence,),
                artifact_store=ArtifactStore(
                    Path(temporary_directory) / "artifacts"
                ),
                subject_digest=SUBJECT_DIGEST,
                git_snapshot=git_snapshot,
            )

        self.assertEqual(
            [entry.evidence_id for entry in plan.entries],
            [evidence.evidence_id],
        )
        self.assertEqual(
            plan.entries[0].content,
            "P-C synthetic CI context: git_snapshot",
        )
