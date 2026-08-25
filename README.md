# CodeMesh V2

> AI writes the change. CodeMesh makes it handover-ready.

CodeMesh V2 is a local-first **Change Handover & Acceptance Workbench**. It does not try to replace Codex, Claude Code, or another coding agent. It sits after code generation and turns a change into an auditable acceptance case: what changed, what evidence supports it, which risks remain, who approved it, and whether the approval still applies to the current code.

The product focuses on questions that ordinary bug checks do not answer:

- Does the implementation still match the original intent and acceptance boundary?
- Did the change introduce architectural debt, a second source of truth, or an undocumented public contract?
- Is the change operable in production, with rollback, telemetry, ownership, and bounded failure behavior?
- Can every review conclusion be traced to immutable evidence?
- Has the code changed since the evidence or human approval was produced?

## Product model

Every review revolves around two objects:

- **Change Acceptance Case** — the immutable subject digest, evidence, policy decisions, reviewer findings, human decisions, and event timeline for one change.
- **Change Passport** — a compact handover result that says whether the exact subject is ready, blocked, or stale, with links to the supporting evidence and decision receipts.

CodeMesh treats model output as review evidence, not authority. Deterministic policy gates own hard constraints; specialist agents inspect different risk dimensions; a human owns the final acceptance decision.

## Current capabilities

The P1-P5 implementation includes:

- Immutable change subjects, normalized digests, and stale-approval invalidation.
- Local evidence collection for Git snapshots, task documents, commands, generic artifacts, and author-agent receipts.
- Artifact integrity checks and an evidence manifest bound to the subject digest.
- Deterministic risk classification and policy gates.
- Three comparison paths: rules-only, one strong reviewer, and a specialist review council.
- Specialist roles for intent, architecture, and operability review.
- Per-agent model routing with tier aliases, priorities, fallback budgets, and execution receipts.
- An adjudicator that can consolidate existing findings but cannot invent new evidence or independently produce `PASS`.
- A local SQLite-backed API and web interface for case queues, evidence, timelines, human decisions, council reports, and Change Passports.

## Review flow

```text
Change + task policy
        |
        v
Immutable subject digest
        |
        v
Evidence collection + manifest
        |
        +--> deterministic risk and policy gates
        |
        +--> rules-only baseline
        +--> single strong reviewer
        +--> specialist review council
        |
        v
Adjudication of existing findings
        |
        v
Human decision
        |
        v
Change Passport
```

Any subject change invalidates evidence and decisions that were bound to the old digest.

## Repository layout

```text
.
├── assurance/   # V2 contracts, collectors, policy, reviewers, routing, reports
├── web/         # FastAPI application, local store, and assurance routes
├── frontend/    # Next.js workbench UI
├── tests/       # Deterministic assurance-domain tests
├── orchestration/, execution/, memory/, feedback/
│                # V1 runtime retained as reusable infrastructure
└── pyproject.toml
```

V2 keeps selected local runtime infrastructure from [CodeMesh V1](https://github.com/Brandoo110/CodeMesh), but it is a different product with a different acceptance-workbench objective.

## Local setup

Requirements:

- Python 3.10+
- Node.js 20+
- pnpm

Install the backend and frontend dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[web,skills,tokens]"

cd frontend
pnpm install
cd ..
```

Run the two local services in separate terminals:

```bash
make ui-backend
make ui-frontend
```

Then open `http://localhost:3010`. The frontend talks to the local API at `http://localhost:8010`.

## Focused verification

The assurance tests are deterministic and do not require real model API calls:

```bash
.venv/bin/python -m pytest tests/test_assurance_contracts.py
.venv/bin/python -m pytest tests/test_assurance_state_machine.py
.venv/bin/python -m pytest tests/test_assurance_model_routing.py
.venv/bin/python -m pytest tests/test_assurance_council_report.py
```

Frontend type-check:

```bash
cd frontend
pnpm exec tsc --noEmit --allowImportingTsExtensions
```

## Evidence boundary

Repository tests and local smoke checks establish implementation-level evidence only. They do not prove that a particular GitHub change, CI run, deployment, or production system has passed acceptance. CodeMesh keeps those evidence layers explicit instead of collapsing them into one green status.

## Project status

P1-P5 form the first runnable V2 product slice. P6 adds the fixed evaluation dataset and promotion gates used to compare rules, a single reviewer, and the specialist council.

## License

No open-source license has been added. This repository is currently intended for private development.
