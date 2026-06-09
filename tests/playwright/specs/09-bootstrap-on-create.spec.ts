import { expect, test } from "@playwright/test";

import { API_URL } from "../playwright.config";
import { cleanupAllProjects, createProject, waitForAgentRun } from "./helpers";

const AUTH = { Authorization: "Bearer local-dev" } as const;

interface PhaseEvent {
  agent_kind: string;
  event_kind: string;
  phase: string | null;
}

interface WikiPageRow {
  title: string;
  status: string;
  page_kind: string;
}

test.describe("Bootstrap-on-create — wiki starts building the moment a project is created", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupAllProjects(request);
    // Creating a project auto-triggers the bootstrap job (no upload, no chat).
    const p = await createProject(request, {
      title: "Sandworm APT infrastructure",
      description: "OSINT on the threat group, its malware families, and notable operations",
    });
    projectId = p.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupAllProjects(request);
  });

  test("a bootstrap run reaches a terminal state and seeds an overview page", async ({
    request,
  }) => {
    const run = await waitForAgentRun(request, projectId, "bootstrap", {
      timeoutMs: 150_000,
      terminalStatus: ["succeeded", "failed"],
    });
    expect(run.status).toBe("succeeded");

    // The scope + seed_overview phases must have streamed as AgentEvents.
    const evResp = await request.get(
      `${API_URL}/v1/projects/${projectId}/agent-events?limit=200`,
      { headers: AUTH },
    );
    expect(evResp.ok()).toBeTruthy();
    const events = (await evResp.json()) as PhaseEvent[];
    const bootPhases = new Set(
      events.filter((e) => e.agent_kind === "bootstrap").map((e) => e.phase),
    );
    expect(bootPhases.has("scope")).toBeTruthy();
    expect(bootPhases.has("seed_overview")).toBeTruthy();

    // An overview wiki page was committed as a draft.
    const pagesResp = await request.get(
      `${API_URL}/v1/projects/${projectId}/wiki/pages`,
      { headers: AUTH },
    );
    expect(pagesResp.ok()).toBeTruthy();
    const pages = (await pagesResp.json()) as WikiPageRow[];
    expect(pages.length).toBeGreaterThanOrEqual(1);
    expect(pages.some((p) => p.status === "draft")).toBeTruthy();
  });
});
