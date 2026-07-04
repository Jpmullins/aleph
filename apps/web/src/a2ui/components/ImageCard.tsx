import { apiUrl } from "@/lib/api";
import { CardShell, isSandboxedAssetSrc, type RendererProps } from "./_shared";

/**
 * WP-4c — renders a code_runner-produced raster/vector image (PNG/SVG) by URI.
 *
 * `src` is a BOUND prop (a project asset streaming-route path); the card never
 * fetches — it just turns the path into an absolute `<img>` source. Images load
 * in an image context, not a document, so the server CSP sandbox on the bytes
 * neutralizes any embedded script. We still require the src to be an asset
 * route (no external/data URLs) for consistency with the amended-rule-8 guard.
 */
export function ImageCard({ component }: RendererProps) {
  const p = component.props as {
    src?: string;
    title?: string;
    alt?: string;
  };
  if (!isSandboxedAssetSrc(p.src)) {
    return (
      <CardShell title={p.title || "Image"}>
        <p className="text-xs text-red-700" data-testid="image-card-refused">
          Refusing to render: image source is not a project asset route.
        </p>
      </CardShell>
    );
  }
  return (
    <CardShell title={p.title || "Image"}>
      <img
        src={apiUrl(p.src)}
        alt={p.alt ?? p.title ?? "Rendered image"}
        className="max-h-[60vh] w-full rounded-md border border-slate-200 bg-white object-contain"
        data-testid="image-card-img"
      />
    </CardShell>
  );
}
