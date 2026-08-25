"""保障域主题摘要单元测试（SubjectDigestInput / 规范化 / 摘要）。

跑法：
    PYTHONPATH=. python -m unittest -v tests.test_assurance_digests
"""

import json
import unittest

from pydantic import ValidationError

import assurance
from assurance import (
    SubjectDigestInput,
    canonical_subject_payload,
    changed_subject_fields,
    compute_normalized_diff_digest,
    compute_subject_digest,
    normalize_line_endings,
    normalize_repo_path,
    normalize_repository_identity,
)
from assurance.contracts import ChangeSubject, PolicyDecision


def _valid_input(**overrides):
    values = {
        "schema_version": "v1",
        "repository": "acme/service",
        "base_revision": "base-abc123",
        "head_revision": "head-def456",
        "normalized_diff_digest": "sha256:" + "a" * 64,
        "task_digest": "sha256:" + "b" * 64,
        "policy_version": "policy-1",
        "rubric_version": "rubric-1",
        "attachment_digests": (),
    }
    values.update(overrides)
    return SubjectDigestInput(**values)


class TestDigestPackageExports(unittest.TestCase):
    def test_all_public_names_exported(self):
        names = (
            "SubjectDigestInput",
            "normalize_repository_identity",
            "normalize_repo_path",
            "normalize_line_endings",
            "compute_normalized_diff_digest",
            "canonical_subject_payload",
            "compute_subject_digest",
            "changed_subject_fields",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIn(name, assurance.__all__)
                self.assertTrue(hasattr(assurance, name))


class TestSubjectDigestInputContract(unittest.TestCase):
    def test_valid_construction_and_json_round_trip(self):
        subject = _valid_input(
            attachment_digests=("sha256:" + "c" * 64, "sha256:" + "d" * 64)
        )
        dumped = subject.model_dump(mode="json")
        self.assertIsInstance(dumped["attachment_digests"], list)
        restored = SubjectDigestInput.model_validate(dumped)
        self.assertEqual(restored, subject)

    def test_repeated_json_serialization_is_stable(self):
        subject = _valid_input(
            attachment_digests=("sha256:" + "c" * 64,)
        )
        self.assertEqual(subject.model_dump_json(), subject.model_dump_json())
        self.assertEqual(
            subject.model_dump(mode="json"), subject.model_dump(mode="json")
        )

    def test_exact_field_order(self):
        self.assertEqual(
            list(_valid_input().model_dump().keys()),
            [
                "schema_version",
                "repository",
                "base_revision",
                "head_revision",
                "normalized_diff_digest",
                "task_digest",
                "policy_version",
                "rubric_version",
                "attachment_digests",
            ],
        )

    def test_schema_version_defaults_to_v1_and_rejects_others(self):
        self.assertEqual(_valid_input().schema_version, "v1")
        values = _valid_input().model_dump()
        values.pop("schema_version")
        self.assertEqual(SubjectDigestInput(**values).schema_version, "v1")
        with self.assertRaises(ValidationError):
            _valid_input(schema_version="v2")

    def test_unknown_field_rejected(self):
        values = _valid_input().model_dump()
        values["unexpected"] = True
        with self.assertRaises(ValidationError):
            SubjectDigestInput.model_validate(values)

    def test_assignment_mutation_rejected(self):
        subject = _valid_input()
        with self.assertRaises(ValidationError):
            subject.repository = "other/repo"

    def test_whitespace_only_identity_fields_rejected(self):
        for field in (
            "repository",
            "base_revision",
            "head_revision",
            "policy_version",
            "rubric_version",
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    _valid_input(**{field: "   "})

    def test_non_string_identity_fields_rejected(self):
        for field in (
            "repository",
            "base_revision",
            "head_revision",
            "policy_version",
            "rubric_version",
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    _valid_input(**{field: 123})

    def test_invalid_digest_rejected(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "g" * 64,
            "md5:" + "a" * 64,
            "sha256:" + "a" * 65,
        )
        for field in ("normalized_diff_digest", "task_digest"):
            for bad in bad_digests:
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValidationError):
                        _valid_input(**{field: bad})

    def test_non_string_digest_rejected(self):
        for field in ("normalized_diff_digest", "task_digest"):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    _valid_input(**{field: 123})

    def test_repository_stored_in_canonical_form(self):
        subject = _valid_input(repository="  Acme\\Service\\  ")
        self.assertEqual(subject.repository, "Acme/Service")

    def test_attachment_input_order_independence_and_sorted_storage(self):
        first = ["sha256:" + "c" * 64, "sha256:" + "a" * 64]
        second = ["sha256:" + "a" * 64, "sha256:" + "c" * 64]
        subject_a = _valid_input(attachment_digests=first)
        subject_b = _valid_input(attachment_digests=second)
        expected = ("sha256:" + "a" * 64, "sha256:" + "c" * 64)
        self.assertEqual(subject_a.attachment_digests, expected)
        self.assertEqual(subject_b.attachment_digests, expected)
        self.assertEqual(subject_a, subject_b)

    def test_attachment_input_copy_safety_and_deep_immutability(self):
        raw = ["sha256:" + "c" * 64, "sha256:" + "a" * 64]
        subject = _valid_input(attachment_digests=raw)
        raw.append("sha256:" + "e" * 64)
        raw[0] = "sha256:" + "e" * 64
        self.assertEqual(
            subject.attachment_digests,
            ("sha256:" + "a" * 64, "sha256:" + "c" * 64),
        )
        with self.assertRaises(TypeError):
            subject.attachment_digests[0] = "sha256:" + "e" * 64

    def test_duplicate_attachment_digest_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_input(
                attachment_digests=[
                    "sha256:" + "a" * 64,
                    "sha256:" + "a" * 64,
                ]
            )

    def test_invalid_attachment_digest_rejected(self):
        bad_digests = (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "g" * 64,
            "md5:" + "a" * 64,
            "sha256:" + "a" * 65,
        )
        for bad in bad_digests:
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _valid_input(attachment_digests=[bad])

    def test_malformed_attachment_container_rejected(self):
        with self.assertRaises(ValidationError):
            _valid_input(attachment_digests="sha256:" + "a" * 64)
        with self.assertRaises(ValidationError):
            _valid_input(attachment_digests={1, 2})
        with self.assertRaises(ValidationError):
            _valid_input(attachment_digests=[123])


class TestNormalizeRepositoryIdentity(unittest.TestCase):
    def test_unicode_nfc(self):
        self.assertEqual(normalize_repository_identity("e\u0301/repo"), "é/repo")

    def test_leading_and_trailing_whitespace_trimmed(self):
        self.assertEqual(
            normalize_repository_identity("  acme/service  "), "acme/service"
        )

    def test_backslashes_replaced(self):
        self.assertEqual(
            normalize_repository_identity("acme\\service"), "acme/service"
        )

    def test_trailing_separators_removed(self):
        self.assertEqual(
            normalize_repository_identity("acme/service///"), "acme/service"
        )

    def test_combined_normalization(self):
        self.assertEqual(
            normalize_repository_identity("  Acme\\Service\\  "),
            "Acme/Service",
        )

    def test_case_preserved(self):
        self.assertEqual(
            normalize_repository_identity("Acme/Service"), "Acme/Service"
        )

    def test_internal_slashes_preserved(self):
        self.assertEqual(normalize_repository_identity("a//b"), "a//b")

    def test_wrong_type_rejected(self):
        for bad in (None, 123, b"acme/service"):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    normalize_repository_identity(bad)

    def test_empty_result_rejected(self):
        for bad in ("", "   ", "///", "  ///  "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalize_repository_identity(bad)


class TestNormalizeRepoPath(unittest.TestCase):
    def test_unicode_nfc(self):
        self.assertEqual(normalize_repo_path("e\u0301/foo"), "é/foo")

    def test_backslashes_replaced(self):
        self.assertEqual(normalize_repo_path("a\\b\\c"), "a/b/c")

    def test_empty_and_dot_segments_ignored(self):
        self.assertEqual(normalize_repo_path("a//./b/"), "a/b")
        self.assertEqual(normalize_repo_path("a/b/."), "a/b")
        self.assertEqual(normalize_repo_path("./a/./b"), "a/b")

    def test_empty_and_dot_only_paths_rejected(self):
        for bad in ("", ".", "./"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalize_repo_path(bad)

    def test_absolute_unc_and_drive_paths_rejected(self):
        for bad in ("/a/b", "//server/share", "C:/foo", "C:\\foo", "c:/foo"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalize_repo_path(bad)

    def test_dotdot_segments_rejected(self):
        for bad in ("a/../b", "../a", "a/..", "a\\..\\b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalize_repo_path(bad)

    def test_nul_rejected(self):
        with self.assertRaises(ValueError):
            normalize_repo_path("a\x00b")

    def test_case_and_legal_characters_preserved(self):
        self.assertEqual(normalize_repo_path("Src/Main.go"), "Src/Main.go")
        self.assertEqual(
            normalize_repo_path("a b#+%.txt"), "a b#+%.txt"
        )

    def test_exact_dotdot_segment_only_rejected(self):
        self.assertEqual(normalize_repo_path("a/.../b"), "a/.../b")

    def test_wrong_type_rejected(self):
        for bad in (None, 123, b"a/b"):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    normalize_repo_path(bad)


class TestNormalizeLineEndings(unittest.TestCase):
    def test_crlf_converted(self):
        self.assertEqual(normalize_line_endings("a\r\nb"), "a\nb")

    def test_lone_cr_converted(self):
        self.assertEqual(normalize_line_endings("a\rb"), "a\nb")

    def test_lf_preserved(self):
        self.assertEqual(normalize_line_endings("a\nb"), "a\nb")

    def test_mixed_endings_converted(self):
        self.assertEqual(
            normalize_line_endings("a\r\nb\rc\nd"), "a\nb\nc\nd"
        )

    def test_trailing_spaces_preserved(self):
        self.assertEqual(normalize_line_endings("a \r\n b \n"), "a \n b \n")

    def test_multiple_final_newlines_preserved(self):
        self.assertEqual(normalize_line_endings("x\r\n\r\n"), "x\n\n")
        self.assertEqual(normalize_line_endings("x\r\r"), "x\n\n")

    def test_no_stripping(self):
        self.assertEqual(normalize_line_endings("  x  "), "  x  ")

    def test_no_unicode_normalization(self):
        self.assertEqual(normalize_line_endings("e\u0301"), "e\u0301")

    def test_wrong_type_rejected(self):
        for bad in (None, 123, b"a\r\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    normalize_line_endings(bad)


class TestComputeNormalizedDiffDigest(unittest.TestCase):
    def test_deterministic_and_sha256_format(self):
        digest = compute_normalized_diff_digest([("a/b", "x\n")])
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            digest, compute_normalized_diff_digest([("a/b", "x\n")])
        )

    def test_input_order_independent(self):
        first = [("b/c", "y"), ("a/b", "x")]
        second = [("a/b", "x"), ("b/c", "y")]
        self.assertEqual(
            compute_normalized_diff_digest(first),
            compute_normalized_diff_digest(second),
        )

    def test_normalized_path_equivalence(self):
        self.assertEqual(
            compute_normalized_diff_digest([("a\\b", "x")]),
            compute_normalized_diff_digest([("a/b", "x")]),
        )

    def test_line_ending_equivalence(self):
        base = compute_normalized_diff_digest([("a", "x\ny")])
        for patch in ("x\r\ny", "x\ry"):
            with self.subTest(patch=patch):
                self.assertEqual(
                    base, compute_normalized_diff_digest([("a", patch)])
                )

    def test_path_change_changes_digest(self):
        self.assertNotEqual(
            compute_normalized_diff_digest([("a", "x")]),
            compute_normalized_diff_digest([("b", "x")]),
        )

    def test_patch_change_changes_digest(self):
        self.assertNotEqual(
            compute_normalized_diff_digest([("a", "x")]),
            compute_normalized_diff_digest([("a", "y")]),
        )

    def test_final_newline_presence_changes_digest(self):
        self.assertNotEqual(
            compute_normalized_diff_digest([("a", "x")]),
            compute_normalized_diff_digest([("a", "x\n")]),
        )

    def test_final_newline_count_changes_digest(self):
        self.assertNotEqual(
            compute_normalized_diff_digest([("a", "x\n")]),
            compute_normalized_diff_digest([("a", "x\n\n")]),
        )

    def test_empty_entries_rejected(self):
        with self.assertRaises(ValueError):
            compute_normalized_diff_digest([])

    def test_malformed_items_rejected(self):
        for bad_entries in (
            [("a", "x", "extra")],
            ["a", "b"],
            [None],
        ):
            with self.subTest(bad_entries=bad_entries):
                with self.assertRaises(ValueError):
                    compute_normalized_diff_digest(bad_entries)

    def test_duplicate_normalized_path_rejected(self):
        with self.assertRaises(ValueError):
            compute_normalized_diff_digest([("a\\b", "x"), ("a/b", "y")])

    def test_illegal_path_rejected(self):
        for bad_path in ("/abs", "a/../b", "C:/foo"):
            with self.subTest(bad_path=bad_path):
                with self.assertRaises(ValueError):
                    compute_normalized_diff_digest([(bad_path, "x")])

    def test_non_string_path_rejected(self):
        with self.assertRaises(TypeError):
            compute_normalized_diff_digest([(1, "x")])

    def test_non_string_patch_rejected(self):
        with self.assertRaises(TypeError):
            compute_normalized_diff_digest([("a", 1)])

    def test_non_sequence_entries_rejected(self):
        with self.assertRaises(TypeError):
            compute_normalized_diff_digest(None)

    def test_user_list_sequence_entries_accepted(self):
        from collections import UserList

        entries = UserList([("a.txt", "x\n")])
        self.assertRegex(
            compute_normalized_diff_digest(entries),
            r"^sha256:[0-9a-f]{64}$",
        )

    def test_string_and_bytes_containers_rejected(self):
        for bad in ("ab", b"ab", bytearray(b"ab")):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    compute_normalized_diff_digest(bad)


class TestCanonicalSubjectPayload(unittest.TestCase):
    def test_stable_bytes(self):
        subject = _valid_input()
        self.assertEqual(
            canonical_subject_payload(subject),
            canonical_subject_payload(subject),
        )

    def test_exact_canonical_bytes(self):
        expected = (
            '{"attachment_digests":[],'
            '"base_revision":"base-abc123",'
            '"head_revision":"head-def456",'
            '"normalized_diff_digest":"sha256:' + "a" * 64 + '",'
            '"policy_version":"policy-1",'
            '"repository":"acme/service",'
            '"rubric_version":"rubric-1",'
            '"schema_version":"v1",'
            '"task_digest":"sha256:' + "b" * 64 + '"}'
        ).encode("utf-8")
        self.assertEqual(
            canonical_subject_payload(_valid_input()), expected
        )

    def test_all_and_only_model_fields(self):
        subject = _valid_input(
            attachment_digests=["sha256:" + "c" * 64, "sha256:" + "a" * 64]
        )
        payload = json.loads(canonical_subject_payload(subject))
        self.assertEqual(
            set(payload.keys()), set(SubjectDigestInput.model_fields)
        )
        self.assertEqual(payload["repository"], subject.repository)
        self.assertEqual(
            payload["attachment_digests"],
            ["sha256:" + "a" * 64, "sha256:" + "c" * 64],
        )

    def test_wrong_type_rejected(self):
        for bad in (None, {}, _valid_input().model_dump()):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    canonical_subject_payload(bad)


class TestComputeSubjectDigest(unittest.TestCase):
    def test_repeated_digest_stable(self):
        subject = _valid_input(
            attachment_digests=["sha256:" + "c" * 64]
        )
        self.assertEqual(
            compute_subject_digest(subject),
            compute_subject_digest(subject),
        )

    def test_each_protected_field_change_alters_digest_and_changed_fields(self):
        cases = {
            "repository": "other/repo",
            "base_revision": "base-xyz",
            "head_revision": "head-xyz",
            "normalized_diff_digest": "sha256:" + "c" * 64,
            "task_digest": "sha256:" + "d" * 64,
            "policy_version": "policy-2",
            "rubric_version": "rubric-2",
            "attachment_digests": ("sha256:" + "e" * 64,),
        }
        for field, new_value in cases.items():
            with self.subTest(field=field):
                before = _valid_input()
                after = _valid_input(**{field: new_value})
                self.assertNotEqual(
                    compute_subject_digest(before),
                    compute_subject_digest(after),
                )
                self.assertEqual(
                    changed_subject_fields(before, after), (field,)
                )

    def test_canonical_repository_spelling_not_reported(self):
        before = _valid_input(repository="acme/service")
        after = _valid_input(repository="acme\\service")
        self.assertEqual(before, after)
        self.assertEqual(
            compute_subject_digest(before), compute_subject_digest(after)
        )
        self.assertEqual(changed_subject_fields(before, after), ())

    def test_attachment_input_order_not_reported(self):
        before = _valid_input(
            attachment_digests=["sha256:" + "c" * 64, "sha256:" + "a" * 64]
        )
        after = _valid_input(
            attachment_digests=["sha256:" + "a" * 64, "sha256:" + "c" * 64]
        )
        self.assertEqual(
            compute_subject_digest(before), compute_subject_digest(after)
        )
        self.assertEqual(changed_subject_fields(before, after), ())

    def test_multiple_changes_reported_in_declaration_order(self):
        before = _valid_input()
        after = _valid_input(
            base_revision="base-xyz",
            policy_version="policy-2",
        )
        self.assertEqual(
            changed_subject_fields(before, after),
            ("base_revision", "policy_version"),
        )
        after = _valid_input(
            repository="other/repo",
            head_revision="head-xyz",
            task_digest="sha256:" + "d" * 64,
        )
        self.assertEqual(
            changed_subject_fields(before, after),
            ("repository", "head_revision", "task_digest"),
        )

    def test_wrong_argument_types_rejected(self):
        subject = _valid_input()
        for bad in (None, {}, subject.model_dump()):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    compute_subject_digest(bad)
        with self.assertRaises(TypeError):
            changed_subject_fields(subject, None)
        with self.assertRaises(TypeError):
            changed_subject_fields("x", subject)


class TestMetadataAndStaleBindingBoundaries(unittest.TestCase):
    def test_metadata_only_differences_do_not_change_subject_digest(self):
        shared = {
            "repository": "acme/service",
            "base_revision": "base-abc123",
            "head_revision": "head-def456",
            "normalized_diff_digest": "sha256:" + "a" * 64,
            "task_digest": "sha256:" + "b" * 64,
            "policy_version": "policy-1",
            "rubric_version": "rubric-1",
        }
        digest = compute_subject_digest(SubjectDigestInput(**shared))
        subject_a = ChangeSubject(
            schema_version="v1",
            change_id="change-a",
            subject_digest=digest,
            repository="acme/service",
            base_revision="base-abc123",
            head_revision="head-def456",
            task_digest="sha256:" + "b" * 64,
            policy_version="policy-1",
            created_at="2026-08-25T02:30:00+08:00",
        )
        subject_b = ChangeSubject(
            schema_version="v1",
            change_id="change-b",
            subject_digest=digest,
            repository="acme/service",
            base_revision="base-abc123",
            head_revision="head-def456",
            task_digest="sha256:" + "b" * 64,
            policy_version="policy-1",
            created_at="2026-08-25T03:30:00+08:00",
        )
        display_title_a = "Subject A display title"
        display_title_b = "Subject B display title"
        self.assertNotEqual(subject_a.change_id, subject_b.change_id)
        self.assertNotEqual(subject_a.created_at, subject_b.created_at)
        self.assertNotEqual(display_title_a, display_title_b)
        self.assertEqual(
            compute_subject_digest(SubjectDigestInput(**shared)), digest
        )
        self.assertEqual(subject_a.subject_digest, digest)
        self.assertEqual(subject_b.subject_digest, digest)
        self.assertNotIn("display_title", ChangeSubject.model_fields)
        self.assertNotIn("display_title", PolicyDecision.model_fields)
        self.assertNotIn("display_title", SubjectDigestInput.model_fields)
        self.assertNotIn("change_id", SubjectDigestInput.model_fields)
        self.assertNotIn("created_at", SubjectDigestInput.model_fields)

    def test_stale_policy_decision_binding_unequal_to_new_digest(self):
        before = _valid_input()
        old_digest = compute_subject_digest(before)
        decision = PolicyDecision(
            schema_version="v1",
            decision_id="decision-001",
            subject_digest=old_digest,
            policy_version="policy-1",
            rules_digest="sha256:" + "c" * 64,
            outcome="PASS",
            evaluated_at="2026-08-25T03:00:00+08:00",
        )
        after = _valid_input(base_revision="base-xyz")
        new_digest = compute_subject_digest(after)
        self.assertEqual(decision.subject_digest, old_digest)
        self.assertNotEqual(old_digest, new_digest)
        self.assertNotEqual(decision.subject_digest, new_digest)


if __name__ == "__main__":
    unittest.main()
