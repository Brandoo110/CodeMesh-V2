"""Server-owned composition for the Assurance Run HTTP boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from assurance.artifacts import ArtifactStore
from assurance.live_freshness import LiveFreshnessChecker
from assurance.run_service import (
    AssuranceRunConfig,
    AssuranceRunService,
    ReviewerContextBuilder,
    ReviewerInvoker,
)
from web.assurance_artifacts import AssuranceArtifactReader
from web.assurance_store import AssuranceWebRepository


@dataclass(frozen=True)
class AssuranceRunWebDependencies:
    """The one Service/Repository pair owned by a Web application instance."""

    service: AssuranceRunService
    repository: AssuranceWebRepository
    artifact_reader: AssuranceArtifactReader | None = None

    def __post_init__(self) -> None:
        """Bind the reader to the Service's already-owned ArtifactStore.

        The runtime lifespan also wraps an already-built service/repository
        pair, so the optional field keeps that compatibility while still
        deriving the exact same store rather than constructing another one.
        """

        if (
            isinstance(self.service, AssuranceRunService)
            and isinstance(self.repository, AssuranceWebRepository)
            and (
                self.repository._live_required is not True
                or self.repository._freshness_checker is None
            )
        ):
            raise ValueError(
                "product run dependencies require a live-required repository"
            )

        if self.artifact_reader is not None:
            if type(self.artifact_reader) is not AssuranceArtifactReader:
                raise TypeError("artifact_reader must be an exact AssuranceArtifactReader")
            artifact_store = getattr(self.service, "_artifact_store", None)
            if artifact_store is not None and (
                self.artifact_reader._repository is not self.repository
                or self.artifact_reader._artifact_store is not artifact_store
            ):
                raise ValueError(
                    "artifact_reader must use the service repository and artifact store"
                )
            return
        artifact_store = getattr(self.service, "_artifact_store", None)
        if artifact_store is not None and type(artifact_store) is not ArtifactStore:
            raise TypeError("service artifact store is invalid")
        if artifact_store is not None:
            object.__setattr__(
                self,
                "artifact_reader",
                AssuranceArtifactReader(self.repository, artifact_store),
            )


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
    freshness_checker = LiveFreshnessChecker(
        workspace_root=config.workspace_root
    )
    repository = AssuranceWebRepository(
        database_path,
        freshness_checker=freshness_checker,
        live_required=True,
    )
    repository.initialize()
    artifact_store = ArtifactStore(artifact_store_root)
    service = AssuranceRunService(
        artifact_store=artifact_store,
        reviewer_invoker=reviewer_invoker,
        committer=repository,
        context_builder=context_builder,
        config=config,
    )
    return AssuranceRunWebDependencies(
        service=service,
        repository=repository,
        artifact_reader=AssuranceArtifactReader(repository, artifact_store),
    )


# Keep the shorter name discoverable for callers that treat this as the
# application's composition root.
build_assurance_run_dependencies = build_assurance_run_web_dependencies


__all__ = [
    "AssuranceRunWebDependencies",
    "build_assurance_run_dependencies",
    "build_assurance_run_web_dependencies",
]
