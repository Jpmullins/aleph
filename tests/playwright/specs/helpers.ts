import { expect, type APIRequestContext, type Page } from "@playwright/test";

const API_URL = process.env.ALEPH_API_BASE_URL ?? "http://localhost:8000";
export const AUTH = { Authorization: "Bearer local-dev" } as const;

/**
 * Every project these specs create carries this prefix, and `cleanupE2EProjects`
 * deletes exactly what carries it. A suite that deletes by "everything that
 * looks recent" eventually deletes somebody's real corpus.
 */
export const E2E_PREFIX = "[e2e] ";

export interface ProjectOut {
  id: string;
  title: string;
  status: string;
}

export async function createProject(
  request: APIRequestContext,
  title: string,
  description = "Playwright e2e project",
): Promise<ProjectOut> {
  const resp = await request.post(`${API_URL}/v1/projects`, {
    headers: { ...AUTH, "Content-Type": "application/json" },
    data: {
      title: title.startsWith(E2E_PREFIX) ? title : `${E2E_PREFIX}${title}`,
      description,
      model_profile_name: "aleph-dev",
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

export async function cleanupE2EProjects(request: APIRequestContext): Promise<void> {
  const resp = await request.get(`${API_URL}/v1/projects`, { headers: AUTH });
  if (!resp.ok()) return;
  const projects = (await resp.json()) as ProjectOut[];
  for (const p of projects.filter((x) => x.title.startsWith(E2E_PREFIX))) {
    await deleteProject(request, p.id);
  }
}

/**
 * Open a project and wait for the shell to be real.
 *
 * The rail is the wait target because it is the one element present for every
 * project regardless of what plugins are installed — the surfaces it lists come
 * from `GET /v1/projects/{id}/panes` and are deliberately not knowable here.
 * The previous version of this helper waited for the text "Sessions", which
 * belonged to a left panel that no longer exists; a spec waiting on deleted
 * markup fails as a 20-second timeout that reads as the app being slow.
 */
export async function openWorkspace(page: Page, projectId: string): Promise<void> {
  const complaints = watchConsole(page);
  await page.goto(`/projects/${projectId}`);
  await page.getByTestId("rail").waitFor({ state: "visible", timeout: 20_000 });
  await page.getByTestId("board").waitFor({ state: "visible", timeout: 20_000 });
  expect(complaints(), "the browser complained while the workspace loaded").toEqual([]);
}

/**
 * Fail the test on anything the browser complains about.
 *
 * The whole Playwright suite had no console listener — the only `console`
 * reference anywhere in it was a `console.log` inside a spec. So every workspace
 * load emitted `renderActivityMessages must be a stable array.` at error level,
 * on every page, for as long as that prop was an inline array literal, and
 * twenty-four green specs said nothing. A React key warning, an unhandled
 * rejection, a failed dynamic import and a thrown render all arrive here and
 * nowhere else.
 *
 * Scope, stated honestly: `openWorkspace` asserts on what was said UP TO the
 * point the board became visible. Errors provoked by a later interaction are
 * not covered unless the spec calls `watchConsole` itself and asserts. That is
 * a real gap; it is also the difference between a listener that exists and one
 * that does not.
 *
 * Two allowances, each because the message is about the harness rather than the
 * app: Vite's HMR chatter, and the "Download the React DevTools" notice.
 */
const CONSOLE_ALLOWED = [/\[vite\]/i, /Download the React DevTools/i];

export function watchConsole(page: Page): () => string[] {
  const complaints: string[] = [];
  page.on("console", (message) => {
    const type = message.type();
    if (type !== "error" && type !== "warning") return;
    const text = message.text();
    if (CONSOLE_ALLOWED.some((re) => re.test(text))) return;
    complaints.push(`console.${type}: ${text}`);
  });
  // A thrown render never reaches `console`; it arrives as a page error, and it
  // is the loudest thing that can happen to a pane.
  page.on("pageerror", (error) => complaints.push(`pageerror: ${error.message}`));
  return () => [...complaints];
}

/** The assistant's composer. CopilotKit owns the markup, so this is a role query. */
export function composer(page: Page) {
  return page.locator(".aleph-live-chat").getByRole("textbox").first();
}

/**
 * Create a session from the dock and wait for the composer to accept typing.
 *
 * Enabled matters as much as visible: the chat renders its textarea before a
 * thread exists, and a spec that types into a disabled composer loses the
 * keystrokes silently and then fails on the assertion after it.
 */
export async function newSession(page: Page): Promise<void> {
  await page.getByTestId("dock-new-session").click();
  const box = composer(page);
  await box.waitFor({ state: "visible", timeout: 30_000 });
  await box.and(page.locator(":not([disabled])")).waitFor({ timeout: 30_000 });
}

export { API_URL };
