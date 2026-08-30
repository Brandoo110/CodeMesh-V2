import json
from types import SimpleNamespace

from typer.testing import CliRunner

from assurance import cli
from assurance.case_publication import PublicationReceipt
import assurance.case_publication as case_publication
import assurance.integrations.github_actions as github_actions
import cli as root_cli


def _args(tmp_path, *command):
    database = tmp_path / "assurance.sqlite"
    artifact_root = tmp_path / "artifacts"
    workspace_root = tmp_path / "workspace"
    database.touch()
    artifact_root.mkdir()
    workspace_root.mkdir()
    return [
        *command,
        "--database",
        str(database),
        "--artifact-root",
        str(artifact_root),
        "--workspace-root",
        str(workspace_root),
        "--case-id",
        "case-1",
    ]


def test_gate_json_is_path_free_and_uses_server_facts(monkeypatch, tmp_path, capsys):
    class FakeEntry:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def gate(self, case_id):
            return {
                "case_id": case_id,
                "gate": "ACCEPTED",
                "freshness": {"status": "FRESH"},
                "allowed_actions": [{"code": "download_passport"}],
            }

    monkeypatch.setattr(cli, "LocalAssuranceEntry", FakeEntry)
    assert cli.main(_args(tmp_path, "gate", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "case_id": "case-1",
        "gate": "ACCEPTED",
        "freshness": {"status": "FRESH"},
        "allowed_actions": [{"code": "download_passport"}],
    }
    assert str(tmp_path) not in capsys.readouterr().err


def test_gate_returns_two_for_valid_but_unaccepted(monkeypatch, tmp_path, capsys):
    class FakeEntry:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def gate(self, case_id):
            return {"case_id": case_id, "gate": "PENDING", "freshness": {"status": "STALE"}, "allowed_actions": []}

    monkeypatch.setattr(cli, "LocalAssuranceEntry", FakeEntry)
    assert cli.main(_args(tmp_path, "gate")) == 2
    assert "PENDING" in capsys.readouterr().out


def test_passport_json_remains_downloadable_when_freshness_unavailable(
    monkeypatch, tmp_path, capsys
):
    passport = {
        "schema": "codemesh.assurance.passport.v1",
        "case_id": "case-1",
        "gate": "ACCEPTED",
        "freshness": {"status": "UNAVAILABLE", "reason_code": "BASELINE_MISSING"},
    }

    class FakeEntry:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def passport(self, case_id, *, format):
            assert case_id == "case-1"
            assert format == "json"
            return passport

    monkeypatch.setattr(cli, "LocalAssuranceEntry", FakeEntry)
    assert cli.main(_args(tmp_path, "passport", "--format", "json")) == 0
    assert capsys.readouterr().out == json.dumps(
        passport, ensure_ascii=False, sort_keys=True
    ) + "\n"


def test_cli_configuration_error_is_generic(monkeypatch, tmp_path, capsys):
    absolute = str(tmp_path / "secret.sqlite")

    class FailingEntry:
        def __init__(self, *args):
            raise RuntimeError(f"private failure at {absolute}")

    monkeypatch.setattr(cli, "LocalAssuranceEntry", FailingEntry)
    assert cli.main(_args(tmp_path, "gate")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "assurance command failed"
    assert absolute not in captured.err


def test_live_run_command_prints_case_subject_gate_freshness_and_workbench(
    monkeypatch, tmp_path
):
    task = tmp_path / "task.md"
    task.write_text("# Task\n", encoding="utf-8")
    receipt = {
        "run_id": "run-001",
        "request_digest": "sha256:" + "2" * 64,
        "cached": False,
        "case_id": "case-001",
        "case_view": {
            "subject_digest": "sha256:" + "1" * 64,
            "policy_gate": {"status": "PENDING"},
            "acceptance_state": "EVIDENCE_COLLECTED",
            "freshness": {"status": "FRESH"},
            "allowed_actions": [],
        },
    }

    def fake_run(**kwargs):
        assert kwargs["repository_identity"] == "acme/widget"
        return receipt

    monkeypatch.setattr(cli, "_perform_live_run", fake_run)
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--repository",
            str(tmp_path),
            "--repository-identity",
            "acme/widget",
            "--base-ref",
            "main",
            "--task-path",
            str(task),
            "--command-id",
            "diff-check",
        ],
    )

    assert result.exit_code == 0
    assert "run-001" in result.stdout
    assert "case-001" in result.stdout
    assert "sha256:" + "1" * 64 in result.stdout
    assert "PENDING" in result.stdout
    assert "EVIDENCE_COLLECTED" in result.stdout
    assert "FRESH" in result.stdout
    assert "http://127.0.0.1:3010/?view=assurance" in result.stdout
    assert str(tmp_path) not in result.stdout


def test_publish_case_command_uses_one_deep_module_and_returns_safe_receipt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(root_cli, "_publish_require_clean", lambda _root: None)
    monkeypatch.setattr(root_cli, "_publish_repository", lambda _root: "acme/codemesh")
    monkeypatch.setattr(root_cli, "_publish_token", lambda: "ghs-secret-token")

    def fake_git(_root, *arguments):
        if arguments[:2] == ("symbolic-ref", "--quiet"):
            return "codex/authoritative-publication"
        if arguments[:2] == ("rev-parse", "HEAD"):
            return "b" * 40
        raise AssertionError(arguments)

    monkeypatch.setattr(root_cli, "_publish_git_value", fake_git)
    captured: dict[str, object] = {}

    class FakeTransport:
        def __init__(self, **kwargs):
            captured["transport"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    class FakePublication:
        def __init__(self, **kwargs):
            captured["publication"] = kwargs

        def publish(self, **kwargs):
            captured["publish"] = kwargs
            return PublicationReceipt(
                schema_version="v1",
                origin="local_authoritative_bundle",
                transport_id="transport-1",
                transport_ref="refs/heads/codex/evidence/" + "a" * 40,
                transport_ref_commit="c" * 40,
                transport_head="b" * 40,
                producer_head="a" * 40,
                repository="acme/codemesh",
                target_pr=2,
                bundle_digest="sha256:" + "1" * 64,
                passport_digest="sha256:" + "2" * 64,
                case_id="case-1",
                run_id="run-1",
                subject_digest="sha256:" + "3" * 64,
                ci_run_id="9001",
                ci_job_id="assurance",
                run_attempt=1,
                artifact_id="artifact-1",
                check_id=123,
                check_url="https://github.com/acme/codemesh/runs/123",
                conclusion="success",
                workbench={},
            )

    monkeypatch.setattr(github_actions, "GitHubActionsTransport", FakeTransport)
    monkeypatch.setattr(case_publication, "CasePublication", FakePublication)
    result = CliRunner().invoke(
        root_cli.app,
        [
            "publish-case",
            "--case-id",
            "case-1",
            "--pr",
            "2",
            "--producer-head",
            "a" * 40,
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["case_id"] == "case-1"
    assert payload["check_id"] == 123
    assert payload["producer_head"] == "a" * 40
    assert "ghs-secret-token" not in result.stdout
    assert captured["publish"] == {
        "case_id": "case-1",
        "target_pr": 2,
        "producer_head": "a" * 40,
    }


def test_publish_case_failure_is_explicitly_unconfirmed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(root_cli, "_publish_require_clean", lambda _root: None)
    monkeypatch.setattr(root_cli, "_publish_repository", lambda _root: "acme/codemesh")
    monkeypatch.setattr(root_cli, "_publish_token", lambda: (_ for _ in ()).throw(ValueError("a GitHub token is required")))
    monkeypatch.setattr(
        root_cli,
        "_publish_git_value",
        lambda _root, *arguments: "codex/authoritative-publication"
        if arguments[:2] == ("symbolic-ref", "--quiet")
        else "b" * 40,
    )
    result = CliRunner().invoke(
        root_cli.app,
        [
            "publish-case",
            "--case-id",
            "case-1",
            "--pr",
            "2",
            "--producer-head",
            "a" * 40,
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["confirmed"] is False
    assert payload["error"] == "ValueError"
    assert "token" in payload["message"]
