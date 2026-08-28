import json

from assurance import cli


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
