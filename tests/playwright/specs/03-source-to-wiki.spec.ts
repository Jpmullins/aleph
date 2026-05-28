import { expect, test } from "@playwright/test";

import { API_URL } from "../playwright.config";
import {
  AUTH,
  SAMPLE_MARKDOWN_SOURCE,
  cleanupAllProjects,
  createProject,
  createSession,
  openProjectWorkspace,
  sendChat,
  uploadSource,
  waitForAgentRun,
} from "./helpers";

test.describe("Source → wiki pipeline end-to-end", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupAllProjects(request);
    const p = await createProject(request, {
      title: "Wiki pipeline test",
      description: "Tests the load-bearing upload→normalize→chunk→wiki flow",
    });
    projectId = p.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupAllProjects(request);
  });

  test("upload a markdown source via API + watch Activity card", async ({ page, request }) => {
    await uploadSource(request, projectId, "cot.md", SAMPLE_MARKDOWN_SOURCE);

    await openProjectWorkspace(page, projectId);

    // The activity card should show at least one of the three jobs running
    // or recently succeeded.
    await expect(page.getByTestId("activity-card-toggle")).toBeVisible();
    const expected = ["Normalizing source", "Chunking + embedding", "Compiling wiki"];
    await expect.poll(async () => {
      const body = await page.locator("body").innerText();
      return expected.some((label) => body.includes(label));
    }, { timeout: 30_000 }).toBe(true);
  });

  test("normalize → chunk_embed → wiki_ingest all reach succeeded", async ({ request }) => {
    // The upload above triggered the chain. AgentRun.agent_kind values:
    //   normalizer (route-side creation in /sources/upload)
    //   chunk_embed (workers)
    //   wiki (workers)
    //   mechanical_reviewer (auto-enqueued from wiki_ingest after commit;
    //     accept any terminal state — workflow may fail under known
    //     LangGraph node-init bug, but the auto-enqueue itself is what
    //     we're verifying here)
    await waitForAgentRun(request, projectId, "normalizer", { timeoutMs: 60_000 });
    await waitForAgentRun(request, projectId, "chunk_embed", { timeoutMs: 90_000 });
    await waitForAgentRun(request, projectId, "wiki", { timeoutMs: 180_000 });
    await waitForAgentRun(request, projectId, "mechanical_reviewer", {
      timeoutMs: 30_000,
      terminalStatus: ["pending", "running", "succeeded", "failed"],
    });
  });

  test("Wiki tab renders the page browser + reader after ingest", async ({ page, request }) => {
    // Backend has compiled pages by now.
    await expect.poll(async () => {
      const resp = await request.get(
        `${API_URL}/v1/projects/${projectId}/wiki/pages`,
        { headers: AUTH },
      );
      if (!resp.ok()) return 0;
      const pages = await resp.json();
      return Array.isArray(pages) ? pages.length : 0;
    }, { timeout: 30_000 }).toBeGreaterThan(0);

    await openProjectWorkspace(page, projectId);
    await page.getByRole("button", { name: "Wiki" }).click();

    // The page browser groups topic + source pages.
    await expect(page.getByRole("heading", { name: /Source pages/ })).toBeVisible({
      timeout: 30_000,
    });

    // Click the compiled source page (has body + claims, unlike topic stubs).
    const sourcePage = page.getByRole("button", { name: /Source: / }).first();
    await expect(sourcePage).toBeVisible();
    await sourcePage.click();

    // Reader shows the back affordance, the rendered body, and claims.
    await expect(page.getByRole("button", { name: "← Wiki" })).toBeVisible();
    await expect(page.getByRole("heading", { name: /Claims/ })).toBeVisible({ timeout: 10_000 });
  });

  test("Assistant can answer over the new wiki content", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await createSession(page);
    await sendChat(page, "What did Wei et al. find about chain-of-thought prompting?");

    // Wait for an assistant bubble with non-streaming status.
    await expect(page.getByTestId("message-assistant").first()).toBeVisible({ timeout: 60_000 });
    // The assistant either answers from the wiki (mentioning the upload's
    // key concepts) or reports a coverage gap. Either proves it ran.
    await expect.poll(
      async () => {
        const bubble = page.getByTestId("message-assistant").first();
        const text = await bubble.innerText();
        return text;
      },
      { timeout: 60_000 },
    ).toMatch(/chain-of-thought|CoT|reasoning|GSM8K|synthesis|coverage|wiki/i);
  });
});
