import type { APIRequestContext, Page } from "@playwright/test";

const API_URL = process.env.ALEPH_API_BASE_URL ?? "http://localhost:8000";
export const AUTH = { Authorization: "Bearer local-dev" } as const;
export const E2E_PREFIX = "[audit-e2e] ";

export interface ProjectOut {
  id: string;
  title: string;
  status: string;
}

export async function createProject(
  request: APIRequestContext,
  title: string,
  description = "Audit e2e project",
): Promise<ProjectOut> {
  const resp = await request.post(`${API_URL}/v1/projects`, {
    headers: { ...AUTH, "Content-Type": "application/json" },
    data: {
      title: title.startsWith(E2E_PREFIX) ? title : `${E2E_PREFIX}${title}`,
      description,
      model_profile_name: "aleph-dev",
      budget_usd: "25.00",
    },
  });
  if (!resp.ok()) throw new Error(`create project failed: ${resp.status()} ${await resp.text()}`);
  return (await resp.json()) as ProjectOut;
}

export async function deleteProject(request: APIRequestContext, id: string): Promise<void> {
  await request.patch(`${API_URL}/v1/projects/${id}`, {
    headers: { ...AUTH, "Content-Type": "application/json" },
    data: { status: "deleted" },
  });
}

export async function cleanupAuditProjects(request: APIRequestContext): Promise<void> {
  const resp = await request.get(`${API_URL}/v1/projects`, { headers: AUTH });
  if (!resp.ok()) return;
  const projects = (await resp.json()) as ProjectOut[];
  for (const p of projects.filter((p) => p.title.startsWith(E2E_PREFIX))) {
    await deleteProject(request, p.id);
  }
}

export async function openWorkspace(page: Page, projectId: string): Promise<void> {
  await page.goto(`/projects/${projectId}`);
  await page.getByText("Sessions").first().waitFor({ state: "visible", timeout: 20_000 });
}

export async function newSession(page: Page): Promise<void> {
  await page.getByRole("button", { name: "+ New" }).first().click();
  await page.getByTestId("copilot-chat-textarea").waitFor({ state: "visible", timeout: 20_000 });
  await page.waitForFunction(
    () => {
      const ta = document.querySelector(
        '[data-testid="copilot-chat-textarea"]',
      ) as HTMLTextAreaElement | null;
      return !!ta && !ta.disabled;
    },
    null,
    { timeout: 20_000 },
  );
}

export { API_URL };
