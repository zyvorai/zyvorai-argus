// Copyright (c) 2026 ZyvorAI Labs Private Limited.
// SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
// Axe (WCAG 2 A/AA) + keyboard check of Mission Control in light and dark.
//   ARGUS_BASE_URL=http://127.0.0.1:8080 node scripts/e2e-dashboard-a11y.mjs
// Exit 1 on any axe violation, a palette that does not trap/restore focus, or a page error.
import { chromium } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const base = process.env.ARGUS_BASE_URL || "http://127.0.0.1:8080";
const browser = await chromium.launch();
let failed = false;
for (const scheme of ["light", "dark"]) {
  const ctx = await browser.newContext({ colorScheme: scheme, viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`${base}/dashboard`);
  await page.waitForTimeout(1200);

  const theme = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  if (theme !== scheme) { console.error(`[${scheme}] expected OS-preferred theme, got ${theme}`); failed = true; }

  await page.keyboard.press("Control+k");
  const focused = await page.evaluate(() => document.activeElement && document.activeElement.id);
  await page.keyboard.press("Tab");
  const afterTab = await page.evaluate(() => document.activeElement && document.activeElement.id);
  await page.keyboard.press("Escape");
  if (focused !== "palette-input" || afterTab !== "palette-input") {
    console.error(`[${scheme}] palette does not hold focus (${focused} → ${afterTab})`); failed = true;
  }

  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  for (const v of results.violations) {
    console.error(`[${scheme}] ${v.impact} ${v.id}: ${v.help} (${v.nodes.length} nodes)`); failed = true;
  }
  if (errors.length) { console.error(`[${scheme}] page errors:`, errors); failed = true; }
  console.log(`[${scheme}] ${results.violations.length} violations, ${errors.length} page errors`);
  await ctx.close();
}
await browser.close();
process.exit(failed ? 1 : 0);
