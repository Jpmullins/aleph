import { createContext, useContext, type ComponentType, type ReactNode, type JSX } from "react";

import { ApprovalCard } from "./components/ApprovalCard";
import { ArtifactsSurface } from "./components/ArtifactsSurface";
import { BriefsSurface } from "./components/BriefsSurface";
import { ChartCard } from "./components/ChartCard";
import { ClaimCard } from "./components/ClaimCard";
import { DiffCard } from "./components/DiffCard";
import { FindingCard } from "./components/FindingCard";
import { FormCard } from "./components/FormCard";
import { GraphCard } from "./components/GraphCard";
import { HypothesesSurface } from "./components/HypothesesSurface";
import { HypothesisCard } from "./components/HypothesisCard";
import { MapCard } from "./components/MapCard";
import { NotebookCellCard } from "./components/NotebookCellCard";
import { NotesSurface } from "./components/NotesSurface";
import { SourceCard } from "./components/SourceCard";
import { TableCard } from "./components/TableCard";
import { WikiSurface } from "./components/WikiSurface";

import type { A2UIComponent, ComponentName } from "./catalog";

type AnyProps = Record<string, unknown>;

interface CommonRenderProps {
  component: A2UIComponent;
  onAction: (action: string, params: AnyProps) => void;
}

const REGISTRY: Record<ComponentName, ComponentType<CommonRenderProps>> = {
  WikiSurface,
  ArtifactsSurface,
  NotesSurface,
  HypothesesSurface,
  BriefsSurface,
  ClaimCard,
  SourceCard,
  ChartCard,
  TableCard,
  MapCard,
  GraphCard,
  ApprovalCard,
  FindingCard,
  HypothesisCard,
  NotebookCellCard,
  FormCard,
  DiffCard,
};

export function renderA2UI(
  component: A2UIComponent,
  onAction: (action: string, params: AnyProps) => void,
): JSX.Element | null {
  const Renderer = REGISTRY[component.type];
  if (!Renderer) {
    return (
      <div className="rounded border border-red-200 bg-red-50 p-3 text-xs text-red-900">
        Unknown A2UI component: {component.type}
      </div>
    );
  }
  return <Renderer component={component} onAction={onAction} />;
}

// Context — components need projectId to fetch live data (dataset rows,
// chart specs, hypothesis evidence, etc.) and to POST feedback. The
// right panel sets this for the entire surface tree.
interface SurfaceCtx {
  projectId: string;
  surface: string;
}

const SurfaceContext = createContext<SurfaceCtx | null>(null);

export function SurfaceProvider({
  projectId,
  surface,
  children,
}: {
  projectId: string;
  surface: string;
  children: ReactNode;
}) {
  return (
    <SurfaceContext.Provider value={{ projectId, surface }}>{children}</SurfaceContext.Provider>
  );
}

export function useSurface(): SurfaceCtx {
  const ctx = useContext(SurfaceContext);
  if (!ctx) {
    throw new Error("useSurface must be used within a SurfaceProvider");
  }
  return ctx;
}
