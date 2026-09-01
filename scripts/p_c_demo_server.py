"""Start the isolated P-C walkthrough server in CI only."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from p_c_handover_walkthrough import _build_fixture
from web.assurance_store import get_assurance_repository
from web.server import create_app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()

    db_path = args.db.resolve()
    fixture = _build_fixture(
        db_path=db_path,
        workspace_root=db_path.parent / "p-c-handover-workspace",
    )
    repository = fixture["repository"]
    app = create_app()
    app.dependency_overrides[get_assurance_repository] = lambda: repository
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
