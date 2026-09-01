<p align="center">
  <strong>English</strong> · <a href="./README.zh-CN.md">简体中文</a>
</p>

# CodeMesh V2

> AI writes the change. CodeMesh makes it handover-ready.

CodeMesh V2 is a local-first change handover and acceptance workbench. It runs after a coding agent has produced a change and turns that exact change into a reviewable record: what changed, which evidence supports it, which risks remain, who made the decision, and whether the decision is still valid for the current code.

CodeMesh is not another coding agent. It is the layer that helps the next person or agent understand, verify, accept, or reject a change.

> **Project status:** the local MVP source flow is integrated on `main`. The project is intended for local evaluation. It has not been deployed or validated for production use.

## What CodeMesh provides

- A stable subject digest for the exact code change under review.
- Evidence collection for Git state, task and policy documents, allowlisted commands, artifacts, and agent receipts.
- Deterministic policy gates for hard constraints.
- One explicitly configured reviewer, currently DeepSeek, with no automatic fallback.
- A local FastAPI and SQLite backend plus a Next.js Workbench.
- Change Acceptance Cases, timelines, findings, human decisions, freshness checks, and exportable Change Passports.
- Optional, separately configured remediation with a bounded workspace grant.

The specialist review council remains an experiment. It is not part of the default review path.

## Default review flow

```text
Code change + task + policy
            |
            v
   Stable subject digest
            |
            v
 Evidence collection and manifest
            |
            +----> deterministic policy gate
            |
            +----> fixed reviewer
            |
            v
 Change Acceptance Case
            |
            v
 Human decision and Change Passport
```

If the subject changes, CodeMesh marks evidence and decisions bound to the old digest as stale or invalid.

## Quick start: offline demo

The offline demo is the fastest way to inspect the Workbench. It uses deterministic local data and does not call a model or write to GitHub.

### Requirements

- Python 3.10 or later
- Node.js 20 or later
- pnpm

### Install

```bash
git clone https://github.com/Brandoo110/CodeMesh-V2.git
cd CodeMesh-V2

make venv
make install
(cd frontend && pnpm install --frozen-lockfile)
```

### Seed the demo

```bash
./.venv/bin/python -m web.assurance_demo
```

The default SQLite file is `~/.codemesh/assurance.sqlite`. The seed is idempotent and refuses to overwrite conflicting fixed cases.

### Start the Workbench

Run the services in separate terminals:

```bash
make ui-backend
```

```bash
make ui-frontend
```

Open [http://localhost:3010](http://localhost:3010). The local API listens on `http://127.0.0.1:8010`; its health endpoint is:

```bash
curl --fail http://127.0.0.1:8010/api/health
```

See [P6_DEMO.md](./P6_DEMO.md) for the short demo walkthrough.

## Run a real local review

A real review calls the configured provider and may incur cost. Run it only when the repository contents and provider call have been authorized.

### 1. Prepare the runtime configuration

```bash
mkdir -p "$HOME/.codemesh/runtime/artifacts"
cp examples/assurance-runtime.v2.example.json \
  "$HOME/.codemesh/runtime/assurance-runtime.v2.json"
```

Edit the copied JSON before starting the service:

- Replace every `ABSOLUTE/PATH` placeholder with an absolute path. In particular,
  set `database_path` to `~/.codemesh/runtime/assurance.sqlite` and
  `artifact_store_root` to `~/.codemesh/runtime/artifacts`, using their fully
  expanded absolute paths in JSON. The command above creates both parent
  directories.
- Keep the database and artifact store outside the reviewed workspace.
- Set `workspace_root` to a directory that contains the target repository.
- Replace remediation `allowed_paths` with real repository-relative paths.
- Keep secrets out of the JSON file.

The run request also needs a task specification and at least one policy file. Start from [examples/quickstart-task.md](./examples/quickstart-task.md). Task and policy paths are relative to the target repository.

### 2. Provide the reviewer secret

For zsh, read the key without echoing it or placing the literal value in shell history:

```bash
read -s 'CODEMESH_REVIEWER_KEY?DeepSeek reviewer API key: '; echo
export CODEMESH_ASSURANCE_REVIEWER_API_KEY="$CODEMESH_REVIEWER_KEY"
unset CODEMESH_REVIEWER_KEY
export CODEMESH_ASSURANCE_CONFIG="$HOME/.codemesh/runtime/assurance-runtime.v2.json"
```

Remediation stays disabled unless its dedicated provider key is also supplied. CodeMesh does not select a fallback provider.

### 3. Start the local API

```bash
./.venv/bin/python -m uvicorn web.server:app \
  --host 127.0.0.1 \
  --port 8010 \
  --workers 1
```

### 4. Submit a change

Run this from the target repository and replace the placeholders:

```bash
/absolute/path/to/CodeMesh-V2/.venv/bin/codemesh assurance run \
  --repository "$PWD" \
  --repository-identity "owner/repository" \
  --author "your-name" \
  --base-ref "<existing-base-ref>" \
  --task-path "path/to/TASK.md" \
  --policy-path "path/to/POLICY.md" \
  --command-id "diff-check" \
  --provider-boundary "within_declared_boundary"
```

The command returns the run ID, case ID, subject digest, policy gate, freshness state, allowed actions, and local Workbench URL. Repeating the same request against an unchanged worktree reuses the deterministic idempotency key.

After the service stops, remove the runtime variables from the shell:

```bash
unset CODEMESH_ASSURANCE_REVIEWER_API_KEY
unset CODEMESH_ASSURANCE_REMEDIATION_API_KEY
unset CODEMESH_ASSURANCE_CONFIG
```

## Repository layout

```text
assurance/     Change subjects, evidence, policy, review, and Passport contracts
web/           FastAPI composition, routes, and local persistence
frontend/      Next.js Workbench
tests/         Python contract and integration tests
scripts/       CI and walkthrough helpers
examples/      Runtime configuration and task templates
```

## Development checks

Useful commands from the repository root:

```bash
make help
git diff --check
(cd frontend && pnpm test)
(cd frontend && pnpm exec tsc --noEmit --allowImportingTsExtensions)
(cd frontend && pnpm lint)
```

## Evidence and safety boundaries

- The product binds evidence and decisions to an exact subject digest.
- Provider secrets belong in environment variables, never in committed files or command arguments.
- Provider errors, missing evidence, invalid configuration, and stale subjects fail closed.
- Offline demos, local tests, synthetic CI, and GitHub Checks do not prove deployment or production acceptance.
- CodeMesh does not deploy code or perform production operations automatically.

## Documentation

- [Offline demo walkthrough](./P6_DEMO.md)
- [Evaluation report and claim boundaries](./P6_EVALUATION_REPORT.md)
- [Runtime configuration example](./examples/assurance-runtime.v2.example.json)
- [Task specification template](./examples/quickstart-task.md)
- [CI handover walkthrough](./docs/p-c-handover-walkthrough.md)

## License

No open-source license has been added. The repository is currently maintained for private development and evaluation.
