import {
  createContext,
  useContext,
  useMemo,
  type ComponentType,
  type ReactNode,
  type JSX,
} from "react";

import { ApprovalCard } from "./components/ApprovalCard";
import { ArtifactCard } from "./components/ArtifactCard";
import { ArtifactsSurface } from "./components/ArtifactsSurface";
import { BriefsSurface } from "./components/BriefsSurface";
import { GroundingSurface } from "./components/GroundingSurface";
import { InspectorSurface } from "./components/InspectorSurface";
import { SettingsSurface } from "./components/SettingsSurface";
import { ChartCard } from "./components/ChartCard";
import { ClaimCard } from "./components/ClaimCard";
import { DiffCard } from "./components/DiffCard";
import { FindingCard } from "./components/FindingCard";
import { FormCard } from "./components/FormCard";
import { HtmlDocCard } from "./components/HtmlDocCard";
import { HtmlFrameCard } from "./components/HtmlFrameCard";
import { ImageCard } from "./components/ImageCard";
import { NoteEditorCard } from "./components/NoteEditorCard";
import { NotesSurface } from "./components/NotesSurface";
import { SourceCard } from "./components/SourceCard";
import { TableCard } from "./components/TableCard";
import { WikiPageCard } from "./components/WikiPageCard";
import { WikiSurface } from "./components/WikiSurface";

import type { A2UIComponent, ComponentName } from "./catalog";

// Context — components need projectId to fetch live data (dataset rows,
// chart specs, claim evidence, etc.) and to POST feedback. The
// right panel sets this for the entire surface tree.
interface SurfaceCtx {
  projectId: string;
  surface: string;
  /**
   * The wire id of the pane this tree is rendering in, or `""` when it is not a
   * pane at all (the chat dock).
   *
   * Here so a surface can open ANOTHER pane of its own kind without naming it.
   * `SettingsSurface` did that with the literal `openPane("Settings")`, which is
   * the same client-side surface name `GET /panes` exists to abolish — and it
   * only worked because "Settings" lower-cases to the registry id `settings`.
   */
  paneKind: string;
}

const SurfaceContext = createContext<SurfaceCtx | null>(null);

export function SurfaceProvider({
  projectId,
  surface,
  paneKind = "",
  children,
}: {
  projectId: string;
  surface: string;
  paneKind?: string;
  children: ReactNode;
}) {
  const value = useMemo(
    () => ({ projectId, surface, paneKind }),
    [projectId, surface, paneKind],
  );
  return <SurfaceContext.Provider value={value}>{children}</SurfaceContext.Provider>;
}

export function useSurface(): SurfaceCtx {
  const ctx = useContext(SurfaceContext);
  if (!ctx) {
    throw new Error("useSurface must be used within a SurfaceProvider");
  }
  return ctx;
}

// ---------------------------------------------------------------------------
// Embedded-child dispatch.
//
// A few surface views (`WikiSurface` embeds, `BriefsSurface` ApprovalCards)
// carry a structural `children` array of A2UIComponent data objects that the
// backend forwards inline (see `aleph_a2ui.components.surfaces`). v0_9's binder
// resolves the surface component's own props but does not walk this Aleph-shaped
// `children` array, so the surface view dispatches each child to its card view
// by `type`. This is the SAME card-view set the v0_9 catalog registers — just
// reached via plain data objects rather than the binder.
// ---------------------------------------------------------------------------

type ChildRenderProps = {
  component: A2UIComponent;
  onAction: (action: string, params: Record<string, unknown>) => void;
};

const CARD_VIEWS: Record<ComponentName, ComponentType<ChildRenderProps>> = {
  WikiSurface,
  ArtifactsSurface,
  NotesSurface,
  BriefsSurface,
  GroundingSurface,
  InspectorSurface,
  SettingsSurface,
  ClaimCard,
  SourceCard,
  ArtifactCard,
  ChartCard,
  ImageCard,
  HtmlFrameCard,
  TableCard,
  ApprovalCard,
  FindingCard,
  FormCard,
  DiffCard,
  WikiPageCard,
  NoteEditorCard,
  HtmlDocCard,
};

export function renderChildCard(
  component: A2UIComponent,
  onAction: (action: string, params: Record<string, unknown>) => void,
): JSX.Element | null {
  const View = CARD_VIEWS[component.type];
  if (!View) {
    return (
      <div className="border border-line bg-badge-failed-bg p-3 text-xs text-badge-failed-fg">
        Unknown A2UI component: {component.type}
      </div>
    );
  }
  return <View component={component} onAction={onAction} />;
}
