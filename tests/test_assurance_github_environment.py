"""Focused offline contract tests for the V2-P8-03 GitHub environment adapter."""

import ast
import inspect
import json

import pytest

from assurance.integrations.github_environment import (
    GitHubEnvironmentDecision,
    GitHubEnvironmentDecisionError,
    GitHubEnvironmentExportResult,
    GitHubEnvironmentExporter,
    GitHubEnvironmentTarget,
    GitHubPendingDeploymentGetIntent,
    GitHubPendingDeploymentPostBody,
    GitHubPendingDeploymentPostIntent,
    GitHubPendingDeploymentReceipt,
)


def _target() -> GitHubEnvironmentTarget:
    return GitHubEnvironmentTarget(owner="acme", repo="widget", run_id=42)


def _decision(**overrides: object) -> GitHubEnvironmentDecision:
    values: dict[str, object] = {
        "decision": "approved",
        "actor": "junjie",
        "comment": "Human approval after assurance review.",
        "environment_ids": (101, 202),
    }
    values.update(overrides)
    return GitHubEnvironmentDecision(**values)


def test_contract_models_are_frozen_and_forbid_extra_fields():
    for model in (
        GitHubEnvironmentTarget,
        GitHubEnvironmentDecision,
        GitHubPendingDeploymentGetIntent,
        GitHubPendingDeploymentPostBody,
        GitHubPendingDeploymentPostIntent,
        GitHubPendingDeploymentReceipt,
        GitHubEnvironmentExportResult,
    ):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"


def test_get_intent_is_constructed_without_external_io():
    result = GitHubEnvironmentExporter.export(_target())

    assert result.get.method == "GET"
    assert (
        result.get.endpoint
        == "/repos/acme/widget/actions/runs/42/pending_deployments"
    )
    assert result.get.body is None
    assert result.post is None
    assert result.receipt.published is False


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_post_intent_requires_explicit_human_decision_and_provider_fields(decision):
    result = GitHubEnvironmentExporter.export(
        _target(), _decision(decision=decision)
    )

    assert result.post is not None
    assert result.post.method == "POST"
    assert result.post.endpoint.endswith("/pending_deployments")
    assert result.post.body.model_dump(mode="json") == {
        "environment_ids": [101, 202],
        "state": decision,
        "comment": "Human approval after assurance review.",
    }
    assert result.post.human_decision.actor == "junjie"
    assert result.receipt.human_actor == "junjie"
    assert result.receipt.human_decision == decision
    assert result.receipt.published is False
    assert "token" not in json.dumps(result.model_dump(mode="json")).lower()


def test_missing_or_unknown_human_approval_fails_closed():
    with pytest.raises(ValueError):
        GitHubEnvironmentExporter.post(_target(), None)

    with pytest.raises(ValueError):
        GitHubEnvironmentExporter.post(
            _target(),
            {
                "decision": "unknown",
                "actor": "junjie",
                "comment": "not enough",
                "environment_ids": [101],
            },
        )


@pytest.mark.parametrize(
    "missing",
    ["actor", "comment", "environment_ids", "decision"],
)
def test_each_human_approval_field_is_required(missing):
    values = {
        "decision": "approved",
        "actor": "junjie",
        "comment": "approved",
        "environment_ids": [101],
    }
    del values[missing]

    with pytest.raises(ValueError):
        GitHubEnvironmentExporter.post(_target(), values)


def test_environment_ids_are_nonempty_positive_unique_strict_integers():
    with pytest.raises(ValueError):
        GitHubEnvironmentDecision(
            decision="approved",
            actor="junjie",
            comment="approved",
            environment_ids=[],
        )
    with pytest.raises(ValueError):
        GitHubEnvironmentDecision(
            decision="approved",
            actor="junjie",
            comment="approved",
            environment_ids=[101, 101],
        )
    with pytest.raises(ValueError):
        GitHubEnvironmentDecision(
            decision="approved",
            actor="junjie",
            comment="approved",
            environment_ids=[True],
        )


@pytest.mark.parametrize("run_id", [True, "42", 0, -1])
def test_workflow_run_id_is_a_positive_strict_integer(run_id):
    with pytest.raises(ValueError):
        GitHubEnvironmentTarget(owner="acme", repo="widget", run_id=run_id)


def test_post_requires_a_decision_even_when_target_is_valid():
    with pytest.raises(GitHubEnvironmentDecisionError):
        GitHubEnvironmentExporter.post(_target())


def test_post_is_byte_stable_for_same_target_and_decision():
    first = GitHubEnvironmentExporter.post(_target(), _decision())
    second = GitHubEnvironmentExporter.post(
        GitHubEnvironmentTarget.model_validate(_target().model_dump()),
        GitHubEnvironmentDecision.model_validate(_decision().model_dump()),
    )
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_post_body_and_receipt_are_fully_bound_and_not_mutable():
    result = GitHubEnvironmentExporter.post(_target(), _decision())
    assert result.post is not None

    with pytest.raises(Exception):
        result.post.body.state = "rejected"

    bad_body = result.post.body.model_copy(update={"state": "rejected"})
    with pytest.raises(ValueError):
        GitHubEnvironmentExportResult(
            get=result.get,
            post=result.post.model_copy(update={"body": bad_body}),
            receipt=result.receipt,
        )

    with pytest.raises(ValueError):
        GitHubEnvironmentExportResult(
            get=result.get.model_copy(update={"endpoint": "/wrong"}),
            post=result.post,
            receipt=result.receipt,
        )

    with pytest.raises(ValueError):
        GitHubEnvironmentExportResult(
            get=result.get,
            post=result.post,
            receipt=result.receipt.model_copy(update={"run_id": 99}),
        )


@pytest.mark.parametrize("owner", ["..", "acme%2fother", "acme\\other", "acme\nother"])
def test_repository_path_parts_cannot_escape_the_offline_endpoint(owner):
    with pytest.raises(ValueError):
        GitHubEnvironmentTarget(owner=owner, repo="widget", run_id=42)


def test_no_network_client_imports_or_http_side_effects():
    import assurance.integrations.github_environment as github_environment

    tree = ast.parse(inspect.getsource(github_environment))
    forbidden = {"httpx", "requests", "urllib", "socket", "aiohttp"}
    imported = {
        node.names[0].name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import) and node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported.isdisjoint(forbidden)
