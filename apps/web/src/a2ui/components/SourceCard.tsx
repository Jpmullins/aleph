import { useState } from "react";

import { useSurface } from "../surface-context";
import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";
import { CardShell, FeedbackButton, Pill, type RendererProps } from "./_shared";

/**
 * WP-4e: SourceCard renders ONLY from bound props — no `useQuery`, no
 * `api.*`, no `fetch`. The normalized-text preview arrives as a bound
 * `normalized_preview` string supplied by the Library surface builder
 * (`_library_messages` → `artifacts_surface_v09`) / the `source_card` builder.
 * The card never self-fetches the normalized-source route.
 */
export function SourceCard({ component, onAction }: RendererProps) {
  const { surface } = useSurface();
  const [reading, setReading] = useState(false);
  const p = component.props as {
    source_id: string;
    short_id: string;
    title: string;
    url?: string | null;
    status: string;
    normalized_preview?: string | null;
    retracted?: boolean;
  };
  const tone =
    p.status === "wiki_done"
      ? "emerald"
      : p.status.includes("failed")
        ? "red"
        : p.status === "indexed"
          ? "sky"
          : "slate";
  const preview = p.normalized_preview ?? "";
  return (
    <CardShell
      title={`${p.short_id} · ${p.title}`}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone={tone}>{p.status}</Pill>
          {(p.retracted || p.status === "retracted") && (
            <Pill tone="red">
              <span data-testid={`source-retracted-${p.source_id}`}>⚠ retracted</span>
            </Pill>
          )}
          {p.url && (
            <a
              href={p.url}
              target="_blank"
              rel="noreferrer"
              className="truncate text-xs text-ink-muted hover:text-ink"
            >
              {p.url}
            </a>
          )}
        </span>
      }
      actions={
        <FeedbackButton
          onAction={onAction}
          targetKind="source"
          targetId={p.source_id}
          surface={surface}
        />
      }
    >
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => onAction("open", { target_id: p.source_id, target_kind: "source" })}
          className="text-xs text-ink-muted hover:text-ink"
        >
          Open source
        </button>
        <button
          type="button"
          onClick={() => onAction("navigate_wiki", { page_id: p.source_id })}
          className="text-xs text-ink-muted hover:text-ink"
        >
          Open source page
        </button>
        {preview && (
          <button
            type="button"
            onClick={() => setReading((r) => !r)}
            className="ml-auto text-xs font-medium text-[var(--accent,#f97316)] hover:opacity-80"
            data-testid={`source-read-${p.source_id}`}
          >
            {reading ? "Hide text ▲" : "Read ▾"}
          </button>
        )}
      </div>
      {reading && preview && (
        <div className="mt-2 max-h-[28rem] overflow-y-auto rounded-md border border-line bg-sunken p-3">
          <WikiBodyMarkdown body={preview} />
        </div>
      )}
    </CardShell>
  );
}
