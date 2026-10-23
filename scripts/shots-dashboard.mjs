// Copyright (c) 2026 ZyvorAI Labs Private Limited.
// SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
// Screenshots of Mission Control for the site, from SAMPLE data only (never a real cluster or run).
// Renders templates/dashboard.html.j2 with python/jinja2, serves it from a fake origin and answers /api/* with fixtures.
//   node scripts/shots-dashboard.mjs [outDir]    (default site/assets)
import { chromium } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { join } from "node:path";

const out = process.argv[2] || "site/assets";
const py = process.env.PYTHON || "python3";
const html = execFileSync(py, ["-c", `
from jinja2 import Environment, FileSystemLoader, ChainableUndefined
e = Environment(loader=FileSystemLoader("templates"), undefined=ChainableUndefined)
print(e.get_template("dashboard.html.j2").render(refresh_interval_ms=0, desktop_mode=False))
`], { encoding: "utf8" });

const now = Date.now();
const iso = (minsAgo) => new Date(now - minsAgo * 60000).toISOString();
const runs = Array.from({ length: 24 }, (_, i) => {
  const when = 38 + i * 95;
  const failed = [3, 11, 17].includes(i) ? 2 : 0;
  const total = 148;
  return { timestamp: iso(when), source: i % 4 === 0 ? "smoke" : "full", passed: total - failed, failed, total,
    duration_s: 212 + (i * 7) % 40, summary: failed ? "2 checkout assertions failed" : "all green", v8_coverage_percentage: 71 + (i % 5) };
});
const fx = {
  "/api/dashboard/overview": {
    status: "go", namespace: "demo", cluster: { available: true }, pod_count: 6,
    workloads: { available: true, deployments: [{ name: "web", ready: 3, desired: 3 }, { name: "api", ready: 2, desired: 2 }, { name: "worker", ready: 1, desired: 1 }], cronjobs: [{ name: "nightly-smoke", schedule: "0 2 * * *", suspend: false, active: false, last_schedule: iso(60 * 9) }] },
    latest_run: runs[0], report_available: true, pdf_available: false, auth: { enabled: false },
    server: { version: "demo", started_at: iso(60 * 30) },
  },
  "/api/dashboard/pods": { available: true, namespace: "demo", pods: ["web-7d9f", "web-4c2a", "web-9b1e", "api-5f6c", "api-8a3d", "worker-2e7b"].map((n) => (
    { name: n, phase: "Running", ready: 1, total: 1, restarts: 0, age_seconds: 86400, node: "node-1", pod_ip: "10.0.0.1", images: ["demo/" + n.split("-")[0] + ":1.4"], warnings: [], usage: null })) },
  "/api/dashboard/runs": { runs },
  "/api/dashboard/jobs/status": { running: false },
  "/api/dashboard/tests": { tests: [
    { title: "checkout › applies discount code", runs: 24, fails: 5, fail_pct: 21, flaky: true },
    { title: "login › remembers the device", runs: 24, fails: 2, fail_pct: 8, flaky: true },
    { title: "search › filters by category", runs: 24, fails: 0, fail_pct: 0, flaky: false } ] },
  "/api/dashboard/knowledge/status": { status: "ok" },
  "/api/v2/intel/quarantine": { quarantine: [] },
};

const browser = await chromium.launch();
for (const scheme of ["dark", "light"]) {
  const ctx = await browser.newContext({ colorScheme: scheme, viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  await page.route("http://argus.local/**", (route) => {
    const p = new URL(route.request().url()).pathname;
    if (p === "/dashboard") return route.fulfill({ contentType: "text/html", body: html });
    route.fulfill({ contentType: "application/json", body: JSON.stringify(fx[p] ?? {}) });
  });
  await page.goto("http://argus.local/dashboard");
  await page.waitForTimeout(1500);
  await page.screenshot({ path: join(out, `dashboard-${scheme}.jpg`), type: "jpeg", quality: 88 });
  await ctx.close();
}
await browser.close();
console.log("wrote", join(out, "dashboard-{dark,light}.jpg"));
