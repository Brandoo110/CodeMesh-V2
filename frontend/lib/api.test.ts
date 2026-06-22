import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, formatApiErrorDetail } from "./api.ts";

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
