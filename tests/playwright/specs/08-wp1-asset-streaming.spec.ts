import { expect, test } from "@playwright/test";

import { cleanupTestProjects, createProject, openProjectWorkspace } from "./helpers";

/**
 * WP-1 — fs asset backend + the one authenticated streaming route.
 *
 * Uploads a PDF through the real UI, opens it in the Library viewer, and
 * asserts the raw bytes arrive via GET /v1/projects/{pid}/assets/source/{sid}
 * (the streaming route) with the stored content-type — no MinIO, no
 * presign hop. Run against a stack booted WITHOUT the s3 profile.
 */

const PDF_BYTES = Buffer.from(
  `%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 62 >> stream
BT /F1 24 Tf 72 700 Td (WP-1 fs asset streaming test) Tj ET
endstream endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
trailer << /Size 6 /Root 1 0 R >>
%%EOF`,
  "utf-8",
);

test.describe("WP-1 asset streaming (fs backend)", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    const project = await createProject(request, {
      title: "WP-1 asset streaming",
      description: "upload → Library viewer via the streaming route",
    });
    projectId = project.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupTestProjects(request);
  });

  test("upload a PDF in the UI and view it via the streaming route", async ({ page }) => {
    await openProjectWorkspace(page, projectId);

    // Upload through the real modal.
    await page.getByRole("button", { name: "+ Upload source" }).click();
    await page
      .locator('input[type="file"]')
      .setInputFiles({ name: "wp1-test.pdf", mimeType: "application/pdf", buffer: PDF_BYTES });
    await page.getByRole("button", { name: "Upload", exact: true }).click();
    // Modal closes only after the POST /sources/upload succeeds.
    await expect(page.getByRole("heading", { name: "Upload source" })).toBeHidden({
      timeout: 15_000,
    });

    // Library tab lists the source.
    await page.getByRole("button", { name: "Library" }).click();
    await expect(page.getByText("wp1-test.pdf").first()).toBeVisible({ timeout: 15_000 });

    // Open the viewer; the iframe must load the streaming route and the
    // response must be the raw PDF (200 + application/pdf), same-origin :8000.
    const assetResponse = page.waitForResponse(
      (r) => r.url().includes("/assets/source/") && r.request().method() === "GET",
      { timeout: 15_000 },
    );
    await page.getByTestId("source-view").first().click();

    const frame = page.getByTestId("source-viewer-frame");
    await expect(frame).toBeVisible();
    const src = await frame.getAttribute("src");
    expect(src).toMatch(new RegExp(`/v1/projects/${projectId}/assets/source/[0-9a-f-]{36}$`));

    const resp = await assetResponse;
    expect(resp.status()).toBe(200);
    expect(resp.headers()["content-type"]).toContain("application/pdf");

    // Iframe document bodies aren't buffered by CDP — re-fetch the same URL
    // and assert the streamed bytes are exactly what was uploaded.
    const direct = await page.request.get(resp.url());
    expect(direct.status()).toBe(200);
    expect((await direct.body()).equals(PDF_BYTES)).toBe(true);

    await page.screenshot({ path: "test-results/wp1-library-viewer.png", fullPage: true });
  });
});
