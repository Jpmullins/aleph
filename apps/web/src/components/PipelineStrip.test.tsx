/**
 * Corpus-level ingest progress.
 *
 * Two deliberate choices, both of which look like bugs if they regress:
 *
 *   - **stages are cumulative.** Counting each status exclusively made sources
 *     appear to LEAVE earlier stages as they advanced, which reads as work
 *     being lost.
 *   - **failures are never folded in.** A corpus that silently shrinks is the
 *     same class of failure as a join that silently returns nothing.
 *
 * And one that is pure UI: with no sources at all the strip renders nothing
 * rather than a row of zeroes, because a row of zeroes on a fresh project reads
 * as a broken pipeline.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor, type RenderResult } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
vi.mock("@/lib/api", () => ({ api: { get: (path: string) => get(path) } }));

import { PipelineStrip } from "@/components/PipelineStrip";

const STAGES = [
  { key: "fetched", label: "Fetched", count: 12 },
  { key: "normalized", label: "Normalized", count: 12 },
  { key: "indexed", label: "Indexed", count: 7 },
];

async function mountStrip(payload: unknown): Promise<RenderResult> {
  get.mockResolvedValue(payload);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <PipelineStrip projectId="proj-1" />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(get).toHaveBeenCalledWith("/v1/projects/proj-1/pipeline"));
  return view;
}

beforeEach(() => {
  get.mockReset();
});

describe("PipelineStrip", () => {
  it("renders nothing on a project with no sources", async () => {
    const view = await mountStrip({ stages: [], failed: 0, total: 0 });
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(view.queryByTestId("pipeline-strip")).toBeNull();
  });

  it("shows every stage the server reported, in order", async () => {
    const view = await mountStrip({ stages: STAGES, failed: 0, total: 12 });
    await waitFor(() => expect(view.getByTestId("pipeline-strip")).toBeTruthy());
    const labels = [...view.getByTestId("pipeline-strip").querySelectorAll("[data-testid^=pipeline-stage-]")]
      .map((n) => n.getAttribute("data-testid"));
    expect(labels).toEqual([
      "pipeline-stage-fetched",
      "pipeline-stage-normalized",
      "pipeline-stage-indexed",
    ]);
  });

  it("reads each stage against the first, so 7 of 12 indexed is legible", async () => {
    const view = await mountStrip({ stages: STAGES, failed: 0, total: 12 });
    await waitFor(() => expect(view.getByTestId("pipeline-stage-indexed")).toBeTruthy());
    expect(view.getByTestId("pipeline-stage-indexed").getAttribute("title")).toBe(
      '7 of 12 sources have reached "Indexed"',
    );
  });

  it("shows failures separately rather than dropping them from the totals", async () => {
    const view = await mountStrip({ stages: STAGES, failed: 3, total: 15 });
    await waitFor(() => expect(view.getByTestId("pipeline-failed")).toBeTruthy());
    expect(view.getByTestId("pipeline-failed").textContent).toBe("3 failed");
  });

  it("says nothing about failures when there are none", async () => {
    const view = await mountStrip({ stages: STAGES, failed: 0, total: 12 });
    await waitFor(() => expect(view.getByTestId("pipeline-strip")).toBeTruthy());
    expect(view.queryByTestId("pipeline-failed")).toBeNull();
  });
});
