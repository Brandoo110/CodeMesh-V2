"""Server-owned composition for the Assurance Run HTTP boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from assurance.artifacts import ArtifactStore
from assurance.run_service import (
    AssuranceRunConfig,
    AssuranceRunService,
    ReviewerContextBuilder,
    ReviewerInvoker,
)
from web.assurance_store import AssuranceWebRepository


@dataclass(frozen=True)
class AssuranceRunWebDependencies:
    """The one Service/Repository pair owned by a Web application instance."""

    service: AssuranceRunService
    repository: AssuranceWebRepository


def build_assurance_run_web_dependencies(
    *,
    database_path: Path,
    artifact_store_root: Path,
    config: AssuranceRunConfig,
    reviewer_invoker: ReviewerInvoker,
    context_builder: ReviewerContextBuilder,
) -> AssuranceRunWebDependencies:
    """Build a durable Run Service and its matching Web Repository.

    All values are explicit server-owned inputs.  In particular, this builder
    does not inspect HTTP requests or read process-wide dotenv configuration.
    """

    if not isinstance(database_path, Path):
        raise TypeError("database_path must be a pathlib.Path")
    if not isinstance(artifact_store_root, Path):
        raise TypeError("artifact_store_root must be a pathlib.Path")
    if type(config) is not AssuranceRunConfig:
        raise TypeError("config must be an exact AssuranceRunConfig")
    if reviewer_invoker is None:
        raise TypeError("reviewer_invoker is required")
    if context_builder is None:
        raise TypeError("context_builder is required")

    # Repository construction and initialization happen exactly once.  The
    # same object is then used as the Service's committer and for CaseView
    # read-back in the HTTP route.
    repository = AssuranceWebRepository(database_path)
    repository.initialize()
    service = AssuranceRunService(
        artifact_store=ArtifactStore(artifact_store_root),
        reviewer_invoker=reviewer_invoker,
        committer=repository,
        context_builder=context_builder,
        config=config,
    )
    return AssuranceRunWebDependencies(service=service, repository=repository)


# Keep the shorter name discoverable for callers that treat this as the
# application's composition root.
build_assurance_run_dependencies = build_assurance_run_web_dependencies


__all__ = [
    "AssuranceRunWebDependencies",
    "build_assurance_run_dependencies",
    "build_assurance_run_web_dependencies",
]
