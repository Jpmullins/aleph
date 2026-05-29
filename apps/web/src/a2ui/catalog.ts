// Mirror of `packages/aleph-a2ui/src/aleph_a2ui/catalog.py`. The web app
// fetches the canonical schema from `GET /v1/a2ui/catalog` at startup
// and stores it here for validation; the static list of component
// names + action names below is the *type* contract.

export const CATALOG_VERSION = "1.0.0";

export const COMPONENT_NAMES = [
  "WikiSurface",
  "ArtifactsSurface",
  "NotesSurface",
  "HypothesesSurface",
  "BriefsSurface",
  "ClaimCard",
  "SourceCard",
  "ArtifactCard",
  "ChartCard",
  "TableCard",
  "MapCard",
  "GraphCard",
  "ApprovalCard",
  "FindingCard",
  "HypothesisCard",
  "NotebookCellCard",
  "FormCard",
  "DiffCard",
] as const;

export type ComponentName = (typeof COMPONENT_NAMES)[number];

export const ACTION_NAMES = [
  "approve",
  "reject",
  "open",
  "navigate_wiki",
  "submit_form",
  "create_hypothesis",
  "edit_note",
  "clarify",
  "mark_handedit",
  "clear_handedit",
] as const;

export type ActionName = (typeof ACTION_NAMES)[number];

export interface A2UIComponent {
  type: ComponentName;
  id: string;
  props: Record<string, unknown>;
  data_bindings?: Record<string, unknown>;
  children?: A2UIComponent[];
}
