import assert from "node:assert/strict";
import fs from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

const baseUrl = process.env.FRONTEND_URL || "http://127.0.0.1:3010";
const outputDir = process.env.WALKTHROUGH_OUTPUT_DIR || ".artifacts/p-c-walkthrough";
await fs.mkdir(outputDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "验收" }).click();
  await page.getByText("Change Queue").waitFor();
  await page.getByRole("button", { name: /P-C handover walkthrough/ }).click();
  await page.getByText("Change Passport · CaseView v1").waitFor();
  await page.getByText("Lineage").waitFor();
  await page.getByText("Findings").waitFor();
  await page.getByText("UNAVAILABLE").waitFor();
  await page.screenshot({ path: `${outputDir}/desktop-case-passport.png`, fullPage: true });

  await page.getByPlaceholder("Owner").fill("release-owner");
  await page.getByPlaceholder(/Role/).fill("release_owner");
  await page.getByPlaceholder("签收理由（必填）").fill("CI walkthrough owner decision");
  await page.getByLabel("我已复核高风险变更并进行二次确认").check();
  await page.getByRole("button", { name: "提交签收" }).click();
  await page.getByText("ACCEPTED").first().waitFor();
  await page.screenshot({ path: `${outputDir}/desktop-readback-accepted.png`, fullPage: true });

  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await mobile.goto(baseUrl, { waitUntil: "networkidle" });
  await mobile.getByRole("button", { name: "验收" }).click();
  await mobile.getByRole("button", { name: /P-C handover walkthrough/ }).click();
  await mobile.getByText("Change Passport · CaseView v1").waitFor();
  const overflow = await mobile.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  assert.equal(overflow, true, "mobile walkthrough must not overflow horizontally");
  await mobile.screenshot({ path: `${outputDir}/mobile-case-passport.png`, fullPage: true });
  await mobile.close();

  await fs.writeFile(
    `${outputDir}/browser-walkthrough.log`,
    "Queue -> Case Passport -> Findings/Lineage/Freshness -> owner decision -> authoritative readback: PASS\n",
  );
  console.log("P-C headless browser walkthrough: PASS");
} finally {
  await browser.close();
}
