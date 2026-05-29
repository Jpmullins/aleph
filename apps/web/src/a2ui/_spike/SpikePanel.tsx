/**
 * Wave 4 Task 1 — de-risking SPIKE (temporary; mounted behind `?spike=1`).
 *
 * Proves the v0_9 shared-catalog -> A2uiSurface pattern end-to-end with ONE
 * Aleph domain card (HypothesisCard) defined in `aleph-catalog-v09.tsx`:
 *
 *   - build a `Catalog` from HypothesisCardImpl + the basicCatalog primitives,
 *   - feed a `MessageProcessor` a hand-authored v0.9 surface message that
 *     places a HypothesisCard with a BINDING for `confidence` ({path:"/h/confidence"})
 *     and literals for the rest, plus a data model { h: { confidence: ... } },
 *   - render it with `<A2uiSurface surface={...} />`,
 *   - mutate `/h/confidence` via `surface.dataModel.set(...)` and watch the card
 *     update reactively.
 *
 * The controller drives the browser to confirm runtime reactivity; this module's
 * job is to compile cleanly and mount the surface.
 */
import { useEffect, useState } from "react";
import { MessageProcessor, Catalog } from "@a2ui/web_core/v0_9";
import { A2uiSurface, basicCatalog } from "@a2ui/react/v0_9";

import { HypothesisCardImpl } from "../aleph-catalog-v09";

const SURFACE_ID = "spike-surface";
const CATALOG_ID = "aleph://v1";

const CONFIDENCE_CYCLE = [
  "under_investigation",
  "likely",
  "well-supported",
  "contested",
];

/**
 * One catalog: Aleph's HypothesisCard impl + every basic-catalog primitive
 * (Text/Column/Row/Card/Button/...) so layout composition works too.
 */
const spikeCatalog = new Catalog(
  CATALOG_ID,
  [HypothesisCardImpl, ...basicCatalog.components.values()],
  [],
);

const initialMessages = [
  {
    version: "v0.9" as const,
    createSurface: { surfaceId: SURFACE_ID, catalogId: CATALOG_ID },
  },
  {
    version: "v0.9" as const,
    updateComponents: {
      surfaceId: SURFACE_ID,
      components: [
        { id: "root", component: "Column", children: ["card"] },
        {
          id: "card",
          component: "HypothesisCard",
          hypothesis_id: "hyp-001",
          title: "The outage was triggered by the 02:14 config push",
          // BINDING: resolved from the data model, mutated by the button below.
          confidence: { path: "/h/confidence" },
          evidence_count: 7,
        },
      ],
    },
  },
  {
    version: "v0.9" as const,
    updateDataModel: {
      surfaceId: SURFACE_ID,
      path: "/",
      value: { h: { confidence: "under_investigation" } },
    },
  },
];

export function SpikePanel() {
  const [processor] = useState(() => {
    const p = new MessageProcessor([spikeCatalog]);
    // The processor's typed message union is built against zod v3; our literal
    // messages match the v0.9 wire shape at runtime. Cast at the boundary.
    p.processMessages(initialMessages as never);
    return p;
  });

  const [surfaces, setSurfaces] = useState(() =>
    Array.from(processor.model.surfacesMap.values()),
  );
  const [confidenceIdx, setConfidenceIdx] = useState(0);

  useEffect(() => {
    const sync = () => setSurfaces(Array.from(processor.model.surfacesMap.values()));
    const createdSub = processor.onSurfaceCreated(sync);
    const deletedSub = processor.onSurfaceDeleted(sync);
    return () => {
      createdSub.unsubscribe();
      deletedSub.unsubscribe();
    };
  }, [processor]);

  const cycleConfidence = () => {
    const next = (confidenceIdx + 1) % CONFIDENCE_CYCLE.length;
    setConfidenceIdx(next);
    const surface = processor.model.surfacesMap.get(SURFACE_ID);
    surface?.dataModel.set("/h/confidence", CONFIDENCE_CYCLE[next]);
  };

  return (
    <aside className="flex w-[28rem] flex-col border-l border-slate-200 bg-white">
      <div className="border-b border-slate-200 p-3">
        <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Wave 4 spike — A2uiSurface
        </div>
        <button
          type="button"
          data-testid="spike-cycle-confidence"
          onClick={cycleConfidence}
          className="mt-2 rounded bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700"
        >
          Cycle confidence → {CONFIDENCE_CYCLE[(confidenceIdx + 1) % CONFIDENCE_CYCLE.length]}
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-3" data-testid="spike-surface">
        {surfaces.length === 0 && (
          <div className="text-sm text-slate-500">Waiting for surface…</div>
        )}
        {surfaces.map((surface) => (
          <A2uiSurface key={surface.id} surface={surface} />
        ))}
      </div>
    </aside>
  );
}
