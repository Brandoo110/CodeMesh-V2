import assert from "node:assert/strict";
import test from "node:test";
import {
  ApiError,
  createAssuranceRun,
  formatApiErrorDetail,
  listAssuranceArtifacts,
  readAssuranceArtifact,
} from "./api.ts";

test("formatApiErrorDetail renders FastAPI validation details", () => {
  const detail = [
    {
      loc: ["body", "name"],
      msg: "Field required",
      type: "missing",
    },
  ];

  assert.equal(formatApiErrorDetail(detail), "body.name: Field required");

  const error = new ApiError(422, detail);
  assert.equal(error.message, "body.name: Field required");
  assert.notEqual(error.message, "[object Object]");
});

test("formatApiErrorDetail keeps object responses readable", () => {
  assert.equal(
    formatApiErrorDetail({ detail: { reason: "duplicate workflow name" } }),
    '{"reason":"duplicate workflow name"}',
  );
});

test("createAssuranceRun always sends the caller idempotency key", async () => {
  const originalFetch = globalThis.fetch;
  let captured: RequestInit | undefined;
  globalThis.fetch = async (_input, init) => {
    captured = init;
    return new Response(JSON.stringify({
      schema_version: "v1",
      run_id: "run-1",
      request_digest: "sha256:" + "1".repeat(64),
      cached: false,
      case_id: "case-1",
      case_view: {},
    }), { status: 201, headers: { "Content-Type": "application/json" } });
  };
  try {
    await createAssuranceRun({
      repository_path: "/tmp/repo",
      repository_identity: "example/service",
      author: "alice",
      base_ref: "main",
      task_path: "TASK.md",
      policy_paths: [],
      adr_paths: [],
      runbook_paths: [],
      command_ids: ["check"],
      changed_lines_total: null,
      external_side_effects: "none_declared",
      provider_boundary: "within_declared_boundary",
    }, "run:key");
    assert.equal((captured?.headers as Record<string, string>)["Idempotency-Key"], "run:key");
    assert.equal(captured?.method, "POST");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("artifact client keeps index JSON and content as plain text", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ input, init });
    if (calls.length === 1) {
      return new Response(JSON.stringify({
        schema_version: "v1",
        case_id: "case-1",
        evidence_id: "evidence-1",
        evidence_kind: "git_snapshot",
        artifacts: [],
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response("diff --git a/a b/b\n+safe\n", {
      status: 200,
      headers: {
        "Content-Type": "text/plain",
        "X-Artifact-Digest": "sha256:" + "2".repeat(64),
        "X-Artifact-Size": "25",
      },
    });
  };
  try {
    const index = await listAssuranceArtifacts("case-1", "evidence-1");
    const content = await readAssuranceArtifact("case-1", "evidence-1", "sha256:" + "2".repeat(64));
    assert.equal(index.case_id, "case-1");
    assert.equal(content.text, "diff --git a/a b/b\n+safe\n");
    assert.equal(content.digest, "sha256:" + "2".repeat(64));
    assert.equal(content.byte_size, 25);
    assert.equal(calls[1].init?.headers && (calls[1].init?.headers as Record<string, string>).Accept, "text/plain");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
