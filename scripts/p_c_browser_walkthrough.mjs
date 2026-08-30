import assert from "node:assert/strict";
import fs from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

const baseUrl = process.env.FRONTEND_URL || "http://127.0.0.1:3010";
const apiBase = process.env.API_BASE || "http://127.0.0.1:8010";
const outputDir = process.env.WALKTHROUGH_OUTPUT_DIR || ".artifacts/p-c-walkthrough";
await fs.mkdir(outputDir, { recursive: true });

async function readApi(path) {
  const response = await fetch(`${apiBase}${path}`);
  const body = await response.json();
  assert.equal(response.ok, true, `${path}: ${response.status}`);
  return body;
}

function stableProjection(value) {
  const result = JSON.parse(JSON.stringify(value));
  if (result.freshness) delete result.freshness.checked_at;
  return result;
}

async function findSeededCases() {
  const queue = await readApi("/api/assurance/changes");
  const details = [];
  for (const item of queue) {
    details.push(await readApi(`/api/assurance/changes/${encodeURIComponent(item.case_id)}`));
  }
  const fresh = details.find(
    (item) => item.freshness?.status === "FRESH"
      && item.allowed_actions.some((action) => action.code === "approve"),
  );
  const unavailable = details.find((item) => item.freshness?.status === "UNAVAILABLE");
  assert.ok(fresh, "seeded FRESH Case with approve action is required");
  assert.ok(unavailable, "seeded UNAVAILABLE Case is required");
  const approve = fresh.allowed_actions.find((action) => action.code === "approve");
  assert.ok(approve.required_human_role, "server must advertise the required owner role");
  return { fresh, unavailable, approve };
}

const browser = await chromium.launch({ headless: true });
try {
  const { fresh, unavailable, approve } = await findSeededCases();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "验收" }).click();
  await page.getByText("Change Queue").waitFor();

  await page.locator("aside button").filter({ hasText: fresh.case_id }).click();
  const main = page.getByRole("main");
  await main.getByText("Change Passport · CaseView v1").waitFor();
  await main.getByText("Lineage").waitFor();
  await main.getByText("Findings").waitFor();
  await main.getByText("Evidence", { exact: true }).waitFor();
  await main.getByText("FRESH", { exact: true }).waitFor();
  await main.getByText("FRESHNESS_MATCH", { exact: true }).waitFor();
  await main.getByText(`Required role: ${approve.required_human_role}`).waitFor();
  await main.getByText("The handover owner must confirm the change boundary.", { exact: true }).click();
  await page.getByText("Authorized Artifacts").waitFor();
  await page.getByRole("button", { name: "关闭证据面板" }).click();
  await page.screenshot({
    path: `${outputDir}/desktop-fresh-before-decision.png`,
    fullPage: true,
  });

  await main.getByPlaceholder("Owner", { exact: true }).fill("handover-owner");
  await main.getByPlaceholder(/Role/).fill(approve.required_human_role);
  await main.getByPlaceholder("签收理由（必填）").fill("CI walkthrough owner decision");
  await main.getByLabel("我已复核高风险变更并进行二次确认").check();
  await main.getByRole("button", { name: "提交签收" }).click();
  await main.getByText("ACCEPTED", { exact: true }).waitFor();
  const accepted = await readApi(`/api/assurance/changes/${encodeURIComponent(fresh.case_id)}`);
  assert.equal(accepted.acceptance_state, "ACCEPTED");
  assert.equal(accepted.freshness.status, "FRESH");
  assert.equal(accepted.case.human_decision_refs.length, 1);
  await page.screenshot({
    path: `${outputDir}/desktop-readback-accepted.png`,
    fullPage: true,
  });

  await page.locator("aside button").filter({ hasText: unavailable.case_id }).click();
  await main.getByText("Change Passport · CaseView v1").waitFor();
  await main.getByText("UNAVAILABLE", { exact: true }).waitFor();
  await main.getByText("当前 Case 没有可执行的 Decision action；只可导出 Passport").waitFor();
  const unavailableBefore = await readApi(
    `/api/assurance/changes/${encodeURIComponent(unavailable.case_id)}`,
  );
  assert.equal(unavailableBefore.freshness.status, "UNAVAILABLE");
  assert.deepEqual(
    unavailableBefore.allowed_actions.map((action) => action.code),
    ["download_passport"],
  );
  const unavailablePostResponse = await fetch(
    `${apiBase}/api/assurance/changes/${encodeURIComponent(unavailable.case_id)}/decisions`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": "p-c-browser-unavailable",
      },
      body: JSON.stringify({
        decision_id: "p-c-browser-unavailable-approve",
        subject_digest: unavailableBefore.subject_digest,
        owner: "handover-owner",
        owner_role: approve.required_human_role,
        decision: "approve",
        reason: "The browser counterexample must fail closed.",
        conditions: [],
        waiver_id: null,
        expires_at: null,
        decided_at: new Date().toISOString(),
        high_risk_confirmed: true,
      }),
    },
  );
  const unavailablePost = await unavailablePostResponse.json();
  assert.equal(unavailablePostResponse.status, 409);
  assert.equal(unavailablePost.detail.code, "ACTION_NOT_ALLOWED");
  const unavailableAfter = await readApi(
    `/api/assurance/changes/${encodeURIComponent(unavailable.case_id)}`,
  );
  assert.deepEqual(stableProjection(unavailableBefore), stableProjection(unavailableAfter));
  assert.deepEqual(unavailableAfter.case.human_decision_refs, []);
  await page.screenshot({
    path: `${outputDir}/desktop-unavailable-no-drift.png`,
    fullPage: true,
  });
  await page.close();

  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await mobile.goto(baseUrl, { waitUntil: "networkidle" });
  await mobile.getByRole("button", { name: "验收" }).click();
  await mobile.locator("aside button").filter({ hasText: fresh.case_id }).click();
  const mobileMain = mobile.getByRole("main");
  await mobileMain.getByText("Change Passport · CaseView v1").waitFor();
  const overflow = await mobile.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth,
  );
  assert.equal(overflow, true, "mobile walkthrough must not overflow horizontally");
  await mobile.screenshot({ path: `${outputDir}/mobile-case-passport.png`, fullPage: true });
  await mobile.close();

  const browserRecord = {
    synthetic_ci_only: true,
    production_evidence: false,
    fresh_case_id: fresh.case_id,
    unavailable_case_id: unavailable.case_id,
    fresh_before_decision: true,
    required_human_role_from_authoritative_get: approve.required_human_role,
    accepted_authoritative_readback: {
      acceptance_state: accepted.acceptance_state,
      freshness: accepted.freshness,
      human_decision_refs: accepted.case.human_decision_refs,
    },
    unavailable_counterexample: {
      post_status: unavailablePostResponse.status,
      post_code: unavailablePost.detail.code,
      authoritative_no_drift: true,
      pre_readback: stableProjection(unavailableBefore),
      post_readback: stableProjection(unavailableAfter),
    },
    evidence_boundary: "headless CI UI journey only; not real CodeMesh dogfood evidence",
  };
  await fs.writeFile(
    `${outputDir}/browser-walkthrough.json`,
    `${JSON.stringify(browserRecord, null, 2)}\n`,
  );
  await fs.writeFile(
    `${outputDir}/browser-walkthrough.log`,
    "Queue -> Case -> Passport/Findings/Evidence/Lineage -> FRESH owner decision -> authoritative GET: PASS\n"
      + "UNAVAILABLE -> current-digest POST 409 ACTION_NOT_ALLOWED -> authoritative GET no drift: PASS\n"
      + "synthetic isolated CI evidence only; not real CodeMesh dogfood evidence\n",
  );
  console.log("P-C headless browser walkthrough: PASS");
} finally {
  await browser.close();
}
