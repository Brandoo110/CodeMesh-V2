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

The current product topology is one fixed strong reviewer plus a deterministic
policy gate. The strict v2 runtime can optionally attach an independently
configured remediation provider; it has no fallback or default model routing.

The local product slice includes:

- Immutable change subjects, normalized digests, and stale-approval invalidation.
- Local evidence collection for Git snapshots, task documents, commands, generic artifacts, and author-agent receipts.
- Artifact integrity checks and an evidence manifest bound to the subject digest.
- Deterministic risk classification and policy gates.
- One strong reviewer at the fixed DeepSeek endpoint, with reviewer provenance and execution receipts.
- An adjudicator that can consolidate existing findings but cannot invent new evidence or independently produce `PASS`.
- A local SQLite-backed API and web interface for case queues, evidence, timelines, human decisions, council reports, and Change Passports.

The specialist council is retained as an unpromoted experiment. It is not the
current production topology or a second default review route.

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

### Offline product demo

Seed the deterministic P6 demo into the local Workbench database, then run the two services above:

```bash
python -m web.assurance_demo
```

The seed is local-only and non-destructive: it adds two fixed acceptance cases, is idempotent, and refuses to overwrite a conflicting fixed case. See [P6_DEMO.md](P6_DEMO.md) for the five-minute walkthrough and [P6_EVALUATION_REPORT.md](P6_EVALUATION_REPORT.md) for the three-arm results, promotion decision, and claim boundaries.

### Real local Change Acceptance quickstart

This is the real local entry point. It is separate from the offline demo and
from fake/deterministic tests: only a run made through this configured runtime
can call the fixed DeepSeek reviewer. The runtime is loopback-only and fails
closed when its configuration, secrets, repository, task/policy files, base
ref, or command IDs are missing or invalid. The fixed DeepSeek reviewer call
itself requires your explicit authorization for this invocation; if you have
not granted that authorization, do not run this quickstart. The same rule
applies to any remediation provider call.

Reviewer operability is deliberately bounded: `max_retries=0` means one
provider request per run. The Case API's `Idempotency-Key` owns replay and
conflict handling; `response_format` requests provider JSON and does not
change that Case API contract. Observability includes reviewer status, schema
status, and errors; prompt, raw-response, and canonical digests; the
`ExecutionReceipt`; and the Case/Passport views. There is no independent
runtime kill-switch: safe stop or rollback means stopping the local service or
rolling back the provider option. Any failure fails closed.

1. Copy the strict v2 example and edit every `ABSOLUTE/PATH` placeholder so it
   names an existing path on this machine:

   ```bash
   cp examples/assurance-runtime.v2.example.json /ABSOLUTE/PATH/TO/assurance-runtime.v2.json
   $EDITOR /ABSOLUTE/PATH/TO/assurance-runtime.v2.json
   ```

   `workspace_root` must be a real directory containing the target Git
   repository; `database_path` and `artifact_store_root` must be separate
   runtime-storage paths outside that workspace. Before starting the server,
   create the database parent and artifact directory, then verify they are
   real, non-symlink directories:

   ```bash
   mkdir -p '/ABSOLUTE/PATH/TO/CODEMESH_RUNTIME' '/ABSOLUTE/PATH/TO/CODEMESH_RUNTIME/artifacts'
   ```

   The configured command IDs must be used by the request. In the request,
   `repository_path` is an absolute path inside `workspace_root`, while
   `task_path`, `policy_paths`, `adr_paths`, and `runbook_paths` are canonical
   repository-relative `.md` paths. The task and every declared policy file
   must already exist. The requested `base_ref` must resolve in that
   repository.

   `task_path` must point to a task spec, not an arbitrary Markdown file such
   as `README.md`. The minimum task-spec contract is a frontmatter block that
   starts on the first line with `---`, ends with a closing `---`, and contains
   non-empty single-line scalar `title:` and `owner:` fields, followed by at
   least one unique Markdown checkbox acceptance criterion. The
   [examples/quickstart-task.md](examples/quickstart-task.md) file is a template
   to copy into the target repository; after copying it, set `task_path` to the
   copied repository-relative path. For CodeMesh self-dogfood, you can use
   `examples/quickstart-task.md` and `.codex/agents/DEFERRED_HARDENING.md`
   directly. The template documents this quickstart change only and does not
   claim reviewer or dogfood success.

   The command spec's `cwd` and remediation `allowed_paths` are intentionally
   repository-relative fields required by their schemas; keep the example's
   `cwd: "."`, replace the absolute runtime path placeholders, and replace
   `allowed_paths` with real paths from the target repository.

   The example fixes the reviewer role to `deepseek` with model
   `deepseek-v4-flash`, the current supported V2 quickstart option. For more
   complex review, you may explicitly select `deepseek-v4-pro`. Legacy
   DeepSeek model names are retired; see the [DeepSeek updates](https://api-docs.deepseek.com/updates/).
   The fixed quickstart transport explicitly disables provider thinking to
   preserve bounded deterministic JSON; even when `deepseek-v4-pro` is chosen
   for complex review, this quickstart remains bounded non-thinking and does
   not claim a dynamic thinking mode.
   It also requests the provider JSON mode (`response_format={"type": "json_object"}`)
   so the bounded deterministic JSON contract is explicit.
   The remediation role is
   explicitly `qwen` and has its own model and policy. Replace its
   `workspace_grant.allowed_paths` with one or more real, existing
   repository-relative paths in the target repository; this field is not an
   absolute filesystem path. You may explicitly change the remediation
   provider, including to `deepseek`, but a DeepSeek remediation call requires
   your authorization for that invocation; it is never a fallback or implicit
   route.

2. Inject secrets only through the two environment variables below. Do not put
   keys in JSON, command arguments, shell history, or committed files:

   ```bash
   read -s 'CODEMESH_REVIEWER_KEY?DeepSeek reviewer key (not echoed): '; echo
   export CODEMESH_ASSURANCE_REVIEWER_API_KEY="$CODEMESH_REVIEWER_KEY"
   unset CODEMESH_REVIEWER_KEY
   read -s 'CODEMESH_REMEDIATION_KEY?Remediation provider key (not echoed): '; echo
   export CODEMESH_ASSURANCE_REMEDIATION_API_KEY="$CODEMESH_REMEDIATION_KEY"
   unset CODEMESH_REMEDIATION_KEY
   export CODEMESH_ASSURANCE_CONFIG='/ABSOLUTE/PATH/TO/assurance-runtime.v2.json'
   ```

   The reviewer key is required. The remediation service starts only when its
   dedicated key is also present; a missing or blank configuration/key fails
   closed rather than selecting another provider. These commands never place
   a secret literal in shell history. After stopping the server, remove the
   exported secrets from the shell:

   ```bash
   unset CODEMESH_ASSURANCE_REVIEWER_API_KEY CODEMESH_ASSURANCE_REMEDIATION_API_KEY CODEMESH_ASSURANCE_CONFIG
   ```

3. Start the local API with one worker:

   ```bash
   .venv/bin/python -m uvicorn web.server:app --host 127.0.0.1 --port 8010 --workers 1
   ```

4. From another terminal, submit one run. Replace all uppercase placeholders;
   `task_path`, `policy_paths`, and the other repository file paths below are
   relative to `repository_path` by contract. The example's `diff-check`
   command is intentionally a lightweight staged/unstaged Git check, not a
   default full test suite:

   ```bash
   curl --fail-with-body -X POST 'http://127.0.0.1:8010/api/assurance/runs' \
     -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: run:LOCAL-UNIQUE-ID' \
     --data-binary '{
       "repository_path": "/ABSOLUTE/PATH/TO/CODEMESH_WORKSPACE/TARGET_REPOSITORY",
       "repository_identity": "example/service",
       "author": "author-agent",
       "base_ref": "BASE_REF_THAT_EXISTS",
       "task_path": "TASK.md",
       "policy_paths": ["POLICY.md"],
       "adr_paths": [],
       "runbook_paths": [],
       "command_ids": ["diff-check"],
       "changed_lines_total": 1,
       "external_side_effects": "none_declared",
       "provider_boundary": "within_declared_boundary"
     }'
   ```

   The response supplies `case_id` and the authoritative `case_view`. The
   endpoint requires a loopback client and an `Idempotency-Key`; reusing a key
   with a different request conflicts rather than silently creating another
   run.

5. Use the existing read surfaces, substituting the IDs returned by the POST:

   ```bash
   # CaseView
   curl --fail 'http://127.0.0.1:8010/api/assurance/changes/CASE_ID'

   # Evidence artifact index
   curl --fail 'http://127.0.0.1:8010/api/assurance/changes/CASE_ID/evidence/EVIDENCE_ID/artifacts'

   # One artifact, using a digest from the artifact index
   curl --fail 'http://127.0.0.1:8010/api/assurance/changes/CASE_ID/evidence/EVIDENCE_ID/artifacts/ARTIFACT_DIGEST'

   # Change Passport as JSON or Markdown
   curl --fail 'http://127.0.0.1:8010/api/assurance/changes/CASE_ID/passport?format=json'
   curl --fail 'http://127.0.0.1:8010/api/assurance/changes/CASE_ID/passport?format=markdown'

   # Existing read-only CLI surfaces
   .venv/bin/python -m assurance.cli gate \
     --database '/ABSOLUTE/PATH/TO/CODEMESH_RUNTIME/assurance.sqlite' \
     --artifact-root '/ABSOLUTE/PATH/TO/CODEMESH_RUNTIME/artifacts' \
     --workspace-root '/ABSOLUTE/PATH/TO/CODEMESH_WORKSPACE' \
     --case-id 'CASE_ID' --json
   .venv/bin/python -m assurance.cli passport \
     --database '/ABSOLUTE/PATH/TO/CODEMESH_RUNTIME/assurance.sqlite' \
     --artifact-root '/ABSOLUTE/PATH/TO/CODEMESH_RUNTIME/artifacts' \
     --workspace-root '/ABSOLUTE/PATH/TO/CODEMESH_WORKSPACE' \
     --case-id 'CASE_ID' --format markdown
   ```

   There is no artifact subcommand in the current CLI; read artifacts through
   the API routes above. A real reviewer/dogfood run requires the actual
   provider call and repository-bound evidence. Offline demos, fake transports,
   and fake tests do not establish that claim.

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

The current status is the local v2 Change Acceptance entry point described
above: one fixed DeepSeek strong reviewer, deterministic policy gate, persisted
case/evidence/passport reads, and optional explicitly configured remediation.
Older P1-P5/P6 labels and evaluation material describe historical slices and do
not mean that the specialist council is currently promoted. At commit
`6fc42037d75600907eef5ed0820ca3a5309c81b2`, local dogfood run `run_d73c...`
for case `case_4665...` used DeepSeek V4 Flash and recorded four successful,
FRESH evidence artifacts, reviewer success with a valid schema, a successful
execution receipt, six findings, two questions, and zero blocking findings.
The resulting gate remains `NEEDS_EVIDENCE/NEEDS_HUMAN`: this is not a
production result or final human approval. The council remains an unpromoted
experiment.

## License

No open-source license has been added. This repository is currently intended for private development.
