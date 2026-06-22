# CodeMesh

CodeMesh is a local-first coding agent runtime for OpenAI-compatible models. It combines Claude Code-style code tools with Dify-style linear workflows, so coding tasks can be routed, executed, reviewed, observed, and resumed from a web interface or CLI.

The project is designed around a small harness architecture: model adapters, tool execution, memory, observability, permissions, and workflow orchestration are separate layers instead of one large prompt script.

## Highlights

- Multi-model routing across DeepSeek, Qwen, Doubao, Gemini, and MiniMax.
- Claude Code-style tools: shell execution, file read/write/edit, glob, grep, AST/LSP navigation, lightweight web search, URL fetch, skills, and memory tools.
- FastAPI + Next.js web UI for chat, model selection, sessions, stats, workflows, run history, diffs, and memory inspection.
- Linear coding workflows with per-step model selection, system prompts, and tool allowlists.
- Review decision loop for Planner/Coder/Reviewer style workflows, including one bounded rework pass.
- Local memory: SQLite facts, auto-extracted markdown memory cards, session journals, and background consolidation.
- Cost and usage tracking in RMB through adapter usage metadata and local call logs.
- Safety controls through command sandboxing, permissions, hooks, and per-step tool restrictions.
- Test suite built with deterministic fakes and no real API calls for normal unit tests.

## Architecture

CodeMesh is organized as four runtime layers plus the web UI:

```text
User task
  |
  v
harness.py
  |
  +-- orchestration/  route, plan, hooks, permissions, plugins, adapters
  +-- execution/      agent loop, tool registry, sandbox, AST/LSP tools
  +-- memory/         short-term, working state, SQLite long-term facts, auto memory
  +-- feedback/       cost, logs, validation, compaction, journal, dreaming, reports
  +-- web/            FastAPI routes, sessions, workflows, memory API
  +-- frontend/       Next.js UI for chat, stats, workflows, diffs, memory
```

### Request Flow

1. `Harness.run()` receives a user task.
2. The router returns a structured model and complexity decision.
3. Memory, skills, and optional retrieval context are injected into the system prompt.
4. Simple tasks run as a single streamed model call.
5. Complex tasks enter the agent loop, where the model can call tools repeatedly.
6. Tool results are returned to the model as normal messages.
7. Cost, usage, traces, local logs, journals, and memory extraction run after the task.

### Workflow Runtime

The workflow engine is implemented in `web/workflow_orchestrator.py`.

Each step gets an isolated `Harness` instance with:

- a preferred model,
- a step-specific system prompt,
- a tool allowlist,
- inherited output from the previous step,
- file diff snapshots before and after execution.

This makes the common coding workflow explicit:

```text
Planner -> Coder -> Reviewer -> optional rework -> final reply
```

## Repository Layout

```text
.
├── cli.py                         # Typer CLI entrypoint
├── harness.py                     # Runtime assembly and task execution
├── pyproject.toml                 # Python package metadata
├── execution/                     # Tool loop, tool registry, sandbox, AST/LSP helpers
├── feedback/                      # Cost, logs, validation, compaction, memory consolidation
├── memory/                        # Short-term, working, long-term, and auto memory
├── orchestration/                 # Router, planner, hooks, plugins, permissions, adapters
├── rag/                           # Optional non-code RAG module
├── web/                           # FastAPI backend
├── frontend/                      # Next.js frontend
└── tests/                         # Unit and web route tests
```

## Requirements

- Python 3.10+
- Node.js 20+
- pnpm or npm for the frontend
- At least one model API key for real runs

Supported model providers use OpenAI-compatible APIs:

- `DEEPSEEK_API_KEY`
- `DASHSCOPE_API_KEY`
- `VOLC_API_KEY`
- `MINIMAX_API_KEY`
- `GEMINI_API_KEY` or `GOOGLE_API_KEY`

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[web,skills,tokens]"
```

Create local environment config:

```bash
cp .env.example .env
```

Then add at least one provider key to `.env`.

## CLI Usage

```bash
codemesh run "explain the harness architecture"
codemesh run "refactor this module and add tests" --stream
codemesh run "compare model outputs for this task" --compare
codemesh stats
codemesh stats --html
```

Optional RAG support is available for non-code text collections:

```bash
pip install -e ".[rag]"
codemesh index .
codemesh run "answer with retrieved context" --rag
```

For source-code navigation, CodeMesh primarily uses agentic search through grep, glob, file reads, and AST/LSP tools instead of vector search.

## Web UI

Install frontend dependencies:

```bash
cd frontend
pnpm install
```

Run backend and frontend in two terminals:

```bash
make ui-backend
make ui-frontend
```

Default local URLs:

- Backend: `http://localhost:8010`
- Frontend: `http://localhost:3010`

Main views:

- Chat: streamed coding assistant with model selection and sessions.
- Stats: local token, cost, and model usage dashboard.
- Workflows: multi-step coding workflows with run history, tool calls, and diffs.
- Memory: local facts, auto memory cards, journals, and consolidation status.

## Local Data

Runtime data is stored under `~/.codemesh/`:

```text
~/.codemesh/
├── calls.jsonl
├── memory.db
├── web_sessions.db
├── workflows.db
├── auto_memory/
└── journal/
```

These files are local runtime state and are not part of the repository.

## Testing

Normal tests do not call real model APIs.

Run backend and core tests:

```bash
for f in tests/test_*.py tests/test_web/test_*.py; do
  [ "$f" = "tests/test_adapters.py" ] && continue
  mod="${f%.py}"
  python -m "${mod//\//.}"
done
```

Run frontend checks:

```bash
cd frontend
pnpm build
node --test lib/*.test.ts
```

Adapter smoke tests can call real providers and may consume API quota:

```bash
python -m tests.test_adapters
```

## Design Notes

CodeMesh keeps the implementation intentionally explicit:

- Tool errors are returned as strings so the model can reason over them.
- The agent loop has a hard iteration limit to avoid runaway tool use.
- File edits prefer targeted replacement over blind overwrite.
- Workflows use per-step tool allowlists to separate planning, writing, and review.
- Memory extraction writes structured markdown cards; consolidation rewrites the index only when gates pass.
- Web UI state is local-first and backed by SQLite.

## License

No open-source license has been added yet.
