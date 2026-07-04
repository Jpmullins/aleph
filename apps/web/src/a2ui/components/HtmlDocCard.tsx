import { apiUrl } from "@/lib/api";
import { isSandboxedAssetSrc, type RendererProps } from "./_shared";

/**
 * WP-4b — renders deterministic server-compiled HTML inside a SANDBOXED iframe.
 *
 * The HTML is produced by the non-LLM `compile_page_html` and served by the
 * asset streaming route (`src` bound prop) with a server `Content-Security-Policy:
 * sandbox` header. The iframe carries `sandbox=""` — NO `allow-same-origin`, NO
 * `allow-scripts` — belt-and-suspenders with the server CSP (the compiled doc has
 * no scripts by construction). The card NEVER fetches: `src` is a bound prop the
 * surface builder computes; here we only turn it into an absolute URL for the
 * browser-native iframe load.
 *
 * Agent-composed dossier/ACH docs pass `derived`/`read_only`.
 */
export function HtmlDocCard({ component }: RendererProps) {
  const p = component.props as {
    src?: string;
    title?: string;
    derived?: boolean;
    read_only?: boolean;
  };
  // Amended rule 8 (WP-4c): mount only if `src` is a principal-boundary,
  // CSP-sandboxed asset/compiled-doc route — never a data:/external URL.
  if (!isSandboxedAssetSrc(p.src)) {
    return (
      <div
        className="rounded border border-red-200 bg-red-50 p-3 text-xs text-red-700"
        data-testid="html-doc-refused"
      >
        Refusing to render: document source is not a project asset streaming route.
      </div>
    );
  }
  const src = apiUrl(p.src);
  return (
    <div className="flex flex-col" data-testid="html-doc-card">
      {p.title && (
        <div className="mb-1 flex items-center gap-2 text-xs text-slate-500">
          <span className="truncate font-medium text-slate-700">{p.title}</span>
          {p.derived && <span className="rounded bg-slate-100 px-1.5 py-0.5">derived</span>}
        </div>
      )}
      <iframe
        title={p.title ?? "Compiled document"}
        src={src}
        sandbox=""
        className="h-[60vh] w-full rounded-md border border-slate-200 bg-white"
        data-testid="html-doc-frame"
      />
    </div>
  );
}
