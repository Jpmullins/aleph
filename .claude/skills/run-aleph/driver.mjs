#!/usr/bin/env node
// Aleph UI driver — drives the running compose stack's web UI via Playwright.
// Reuses the Playwright install at tests/playwright (no extra deps).
//
// Usage (from repo root, stack must already be up):
//   node .claude/skills/run-aleph/driver.mjs smoke
//   node .claude/skills/run-aleph/driver.mjs mkproject ["title"]
//   node .claude/skills/run-aleph/driver.mjs shot /                 [out.png]
//   node .claude/skills/run-aleph/driver.mjs workspace [projectId]  [out.png]
//   node .claude/skills/run-aleph/driver.mjs chat "message"         [projectId]
//
// Screenshots default to /tmp/aleph-<cmd>.png. Exit code 0 = flow worked.

import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const require = createRequire(
  new URL("../../../tests/playwright/package.json", import.meta.url),
);
const { chromium } = require("@playwright/test");

// On hosts without root (e.g. this WSL2 box), Chromium's shared-lib deps
// (libnspr4/libnss3/…) can't be apt-installed system-wide. We stage them in
// ~/.cache/ms-playwright/aleph-syslibs and point the browser at them via
// LD_LIBRARY_PATH. No-op when the dir is absent (deps installed normally).
function launchEnv() {
  const syslibs = join(homedir(), ".cache", "ms-playwright", "aleph-syslibs");
  if (!existsSync(syslibs)) return process.env;
  const prev = process.env.LD_LIBRARY_PATH;
  return { ...process.env, LD_LIBRARY_PATH: prev ? `${syslibs}:${prev}` : syslibs };
}

const WEB = process.env.ALEPH_WEB_BASE_URL ?? "http://localhost:5173";
const API = process.env.ALEPH_API_BASE_URL ?? "http://localhost:8000";
const AUTH = { Authorization: "Bearer local-dev" };

const [cmd, ...args] = process.argv.slice(2);

async function api(path) {
  const r = await fetch(`${API}${path}`, { headers: AUTH });
  if (!r.ok) throw new Error(`${path} -> ${r.status} ${await r.text()}`);
  return r.json();
}

async function firstProjectId() {
  const projects = await api("/v1/projects");
  if (!projects.length) throw new Error("no projects — create one in the UI or via POST /v1/projects");
  return projects[0].id;
}

async function withPage(fn) {
  const browser = await chromium.launch({ env: launchEnv() });
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text().slice(0, 200)));
  page.on("requestfailed", (rq) => errors.push(`REQFAIL ${rq.url().slice(0, 120)}`));
  try {
    await fn(page);
  } finally {
    if (errors.length) console.log("console/network errors:", JSON.stringify(errors.slice(0, 10), null, 1));
    await browser.close();
  }
}

async function openWorkspace(page, projectId) {
  await page.goto(`${WEB}/projects/${projectId}`);
  await page.waitForSelector("text=Sessions", { timeout: 20_000 });
}

const commands = {
  async smoke() {
    // Containers report Started well before the apps listen (api ~20-30s
    // cold) — poll each endpoint until the shared deadline.
    const deadline = Date.now() + 90_000;
    const checks = [
      ["api /healthz", `${API}/healthz`],
      ["web", WEB],
      ["copilot-runtime", "http://localhost:4000/api/copilotkit"],
    ];
    let failed = 0;
    for (const [name, url] of checks) {
      let status;
      for (;;) {
        status = await fetch(url, { method: "GET" }).then((r) => r.status).catch((e) => `DOWN (${e.cause?.code ?? e.message})`);
        // copilot-runtime has no health route; any HTTP status (even 404) means the process is up
        const ok = typeof status === "number" && (name === "copilot-runtime" ? true : status < 400);
        if (ok || Date.now() > deadline) {
          console.log(`${ok ? "✓" : "✗"} ${name} -> ${status}`);
          if (!ok) failed++;
          break;
        }
        await new Promise((r) => setTimeout(r, 3000));
      }
    }
    try {
      const projects = await api("/v1/projects");
      console.log(`✓ api auth (Bearer local-dev) -> ${projects.length} projects`);
    } catch (e) {
      console.log(`✗ api auth (Bearer local-dev) -> ${e.message.slice(0, 120)}`);
      failed++;
    }
    process.exitCode = failed ? 1 : 0;
  },

  async mkproject(title = "Driver test project") {
    const r = await fetch(`${API}/v1/projects`, {
      method: "POST",
      headers: { ...AUTH, "Content-Type": "application/json" },
      // `ProjectCreate` is `extra="forbid"` — an obsolete field is a 422, not
      // an ignored key. `budget_usd` went with the `budgets` table.
      body: JSON.stringify({
        title,
        description: "Created by run-aleph driver",
        model_profile_name: "aleph-dev",
      }),
    });
    if (!r.ok) throw new Error(`POST /v1/projects -> ${r.status} ${await r.text()}`);
    const p = await r.json();
    console.log(`✓ created project "${p.title}" ${p.id}`);
    return p.id;
  },

  async shot(path = "/", out = "/tmp/aleph-shot.png") {
    await withPage(async (page) => {
      await page.goto(`${WEB}${path}`);
      await page.waitForTimeout(3000);
      await page.screenshot({ path: out });
      console.log(`title: ${await page.title()}\nscreenshot: ${out}`);
    });
  },

  async workspace(projectId, out = "/tmp/aleph-workspace.png") {
    projectId ||= await firstProjectId();
    await withPage(async (page) => {
      await openWorkspace(page, projectId);
      await page.waitForTimeout(2000);
      // The surface switcher is the left Rail (`SURFACE_TABS` in
      // apps/web/src/lib/workspace-ui.tsx). Probe it by testid rather than by
      // label: several surfaces render their own like-named buttons, so a
      // by-name lookup can report a surface as reachable when it is not.
      for (const tab of ["Wiki", "Library", "Notes", "Hypotheses", "Briefs"]) {
        const visible = await page
          .getByTestId(`rail-${tab.toLowerCase()}`)
          .isVisible()
          .catch(() => false);
        console.log(`${visible ? "✓" : "✗"} rail: ${tab}`);
      }
      await page.screenshot({ path: out });
      console.log(`screenshot: ${out}`);
    });
  },

  async chat(message, projectId, out = "/tmp/aleph-chat.png") {
    if (!message) throw new Error('usage: driver.mjs chat "message" [projectId]');
    projectId ||= await firstProjectId();
    await withPage(async (page) => {
      await openWorkspace(page, projectId);
      // Session creation moved from the (now unrendered) LeftPanel into the
      // AssistantDock; "+ New" is no longer unique on the page.
      await page.getByTestId("dock-new-session").click();
      const composer = page.getByTestId("copilot-chat-textarea");
      await composer.waitFor({ state: "visible", timeout: 15_000 });
      await page.waitForTimeout(2000); // composer enables once the AG-UI thread resolves
      await composer.click();
      await composer.pressSequentially(message);
      await page.getByTestId("copilot-send-button").click();
      console.log("sent; polling transcript (orchestrator turns take 15–90s)...");
      const start = Date.now();
      let text = "";
      while (Date.now() - start < 150_000) {
        await page.waitForTimeout(5000);
        text = await page.getByTestId("copilot-message-list").innerText().catch(() => "");
        const reply = text.replace(message, "").trim();
        if (reply.length > 0) break;
      }
      await page.screenshot({ path: out });
      console.log(`--- transcript ---\n${text.slice(0, 1200)}\n--- screenshot: ${out}`);
      if (text.replace(message, "").trim().length === 0) {
        console.error("✗ no assistant reply rendered");
        process.exitCode = 1;
      }
    });
  },
};

if (!commands[cmd]) {
  console.error(`unknown command: ${cmd ?? "(none)"}\ncommands: ${Object.keys(commands).join(", ")}`);
  process.exit(2);
}
await commands[cmd](...args);
