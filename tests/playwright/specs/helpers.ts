import type { APIRequestContext, Page } from "@playwright/test";

import { API_URL } from "../playwright.config";

const AUTH = { Authorization: "Bearer local-dev" } as const;

export interface ProjectOut {
  id: string;
  title: string;
  description: string;
  status: "active" | "archived" | "deleted";
  model_profile_id: string;
  created_at: string;
}

/** Delete every active project (status != deleted). Use in beforeAll/afterAll. */
export async function cleanupAllProjects(request: APIRequestContext): Promise<void> {
  const resp = await request.get(`${API_URL}/v1/projects`, { headers: AUTH });
  if (!resp.ok()) return;
  const projects = (await resp.json()) as ProjectOut[];
  for (const p of projects) {
    await request.patch(`${API_URL}/v1/projects/${p.id}`, {
      headers: { ...AUTH, "Content-Type": "application/json" },
      data: { status: "deleted" },
    });
  }
}

export async function createProject(
  request: APIRequestContext,
  opts: { title: string; description?: string; budget_usd?: string } = { title: "Test" },
): Promise<ProjectOut> {
  const resp = await request.post(`${API_URL}/v1/projects`, {
    headers: { ...AUTH, "Content-Type": "application/json" },
    data: {
      title: opts.title,
      description: opts.description ?? "Playwright test project",
      model_profile_name: "aleph-dev",
      budget_usd: opts.budget_usd ?? "100.00",
    },
  });
  if (!resp.ok()) {
    throw new Error(`Failed to create project: ${resp.status()} ${await resp.text()}`);
  }
  return (await resp.json()) as ProjectOut;
}

export async function openProjectWorkspace(page: Page, projectId: string): Promise<void> {
  await page.goto(`/projects/${projectId}`);
  await page.waitForSelector("text=Sessions", { timeout: 15_000 });
}

export async function createSession(page: Page): Promise<void> {
  await page.getByRole("button", { name: "+ New" }).first().click();
  // Wait for the composer to enable (means thread resolved).
  await page.getByTestId("chat-composer").waitFor({ state: "visible" });
  await page.waitForFunction(
    () => {
      const ta = document.querySelector('[data-testid="chat-composer"]') as HTMLTextAreaElement | null;
      return !!ta && !ta.disabled;
    },
    null,
    { timeout: 15_000 },
  );
}

export async function sendChat(page: Page, text: string): Promise<void> {
  const composer = page.getByTestId("chat-composer");
  await composer.fill(text);
  await composer.press("Enter");
}

export async function uploadSource(
  request: APIRequestContext,
  projectId: string,
  filename: string,
  content: string,
): Promise<{ source_id: string }> {
  // The upload endpoint accepts multipart/form-data.
  const resp = await request.post(`${API_URL}/v1/projects/${projectId}/sources/upload`, {
    headers: AUTH,
    multipart: {
      file: {
        name: filename,
        mimeType: "text/markdown",
        buffer: Buffer.from(content, "utf-8"),
      },
    },
  });
  if (!resp.ok()) {
    throw new Error(`Upload failed: ${resp.status()} ${await resp.text()}`);
  }
  return (await resp.json()) as { source_id: string };
}

interface AgentRunOut {
  id: string;
  agent_kind: string;
  status: string;
  completed_at: string | null;
}

/** Poll the agent-runs endpoint until a run of `kind` reaches `terminalStatus`. */
export async function waitForAgentRun(
  request: APIRequestContext,
  projectId: string,
  kind: string,
  options: { timeoutMs?: number; terminalStatus?: string[] } = {},
): Promise<AgentRunOut> {
  const timeout = options.timeoutMs ?? 90_000;
  const terminal = new Set(options.terminalStatus ?? ["succeeded", "failed", "cancelled"]);
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const resp = await request.get(
      `${API_URL}/v1/projects/${projectId}/agent-runs?limit=50`,
      { headers: AUTH },
    );
    if (resp.ok()) {
      const runs = (await resp.json()) as AgentRunOut[];
      const match = runs.find((r) => r.agent_kind === kind && terminal.has(r.status));
      if (match) return match;
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  throw new Error(`Timeout waiting for ${kind} run after ${timeout}ms`);
}

export const SAMPLE_MARKDOWN_SOURCE = `# Chain-of-Thought Prompting in Large Language Models

Wei et al. (2022) demonstrated that prompting LLMs with intermediate
reasoning steps — "Chain-of-Thought" (CoT) prompts — substantially
improves performance on multi-step reasoning benchmarks.

## Key findings

- On GSM8K, a math word problem benchmark, CoT prompting with PaLM 540B
  achieves 56.9% solve rate, up from 17.9% with standard prompting — a
  39 point absolute improvement.
- The benefit emerges only at sufficient model scale. Models below
  ~62B parameters showed little improvement from CoT.
- CoT generalizes across arithmetic, commonsense reasoning, and symbolic
  manipulation tasks.

## Mechanism

CoT prompting works by allocating more compute per token at inference
time. By generating intermediate steps, the model effectively decomposes
the problem and applies its capacity to each sub-step.

## Related work

Self-consistency (Wang et al., 2022) further improves CoT results by
sampling multiple reasoning paths and voting. Tree-of-Thoughts (Yao et
al., 2023) extends to tree search over reasoning steps.

## Limitations

CoT improves accuracy but does not eliminate hallucination. Faithful
intermediate steps are not guaranteed even when the final answer is
correct.
`;

export { AUTH };
