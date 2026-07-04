import { apiUrl } from "@/lib/api";
import { CardShell, isSandboxedAssetSrc, type RendererProps } from "./_shared";

/**
 * WP-4c — renders INTERACTIVE, self-contained HTML (a code_runner artifact)
 * inside a sandboxed iframe.
 *
 * Two layers enforce the amended rule 8:
 *   1. Element sandbox `allow-scripts` — scripts run, but the frame is an opaque
 *      origin: NO `allow-same-origin` (can't read the API origin's cookies/auth)
 *      and NO network-granting tokens.
 *   2. Server CSP — the asset streaming route serves html_frame bytes with
 *      `Content-Security-Policy: sandbox allow-scripts`, the belt to the iframe's
 *      suspenders (and it also covers a direct URL open).
 * The card NEVER fetches, and it refuses to mount unless `src` is a project
 * asset streaming route (no data:/external/agent-arbitrary URLs).
 */
export function HtmlFrameCard({ component }: RendererProps) {
  const p = component.props as {
    src?: string;
    title?: string;
  };
  if (!isSandboxedAssetSrc(p.src)) {
    return (
      <CardShell title={p.title || "Interactive"}>
        <p className="text-xs text-red-700" data-testid="html-frame-refused">
          Refusing to render: frame source is not a project asset streaming route.
        </p>
      </CardShell>
    );
  }
  return (
    <div className="flex flex-col" data-testid="html-frame-card">
      {p.title && (
        <div className="mb-1 truncate text-xs font-medium text-slate-700">{p.title}</div>
      )}
      <iframe
        title={p.title ?? "Interactive artifact"}
        src={apiUrl(p.src)}
        sandbox="allow-scripts"
        className="h-[60vh] w-full rounded-md border border-slate-200 bg-white"
        data-testid="html-frame-iframe"
      />
    </div>
  );
}
