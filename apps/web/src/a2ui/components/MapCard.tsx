import { useEffect, useRef } from "react";

import { CardShell, Pill, type RendererProps } from "./_shared";

interface GeoPoint {
  lat: number;
  lon: number;
  label?: string;
  value?: number;
}

export function MapCard({ component }: RendererProps) {
  const p = component.props as {
    title?: string;
    points?: GeoPoint[];
    style_url?: string;
    center?: [number, number];
    zoom?: number;
    _placeholder?: boolean;
  };
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || p._placeholder || !p.points || p.points.length === 0) return;
    let map: { remove: () => void } | null = null;
    let disposed = false;
    void Promise.all([
      import("maplibre-gl"),
      import("maplibre-gl/dist/maplibre-gl.css" as never).catch(() => null),
    ]).then(([mod]) => {
      if (disposed) return;
      const maplibregl = (mod as { default?: unknown }).default ?? mod;
      const m = maplibregl as typeof import("maplibre-gl");
      const lats = (p.points ?? []).map((pt) => pt.lat);
      const lons = (p.points ?? []).map((pt) => pt.lon);
      const center: [number, number] =
        p.center ?? [
          lons.reduce((a, b) => a + b, 0) / Math.max(1, lons.length),
          lats.reduce((a, b) => a + b, 0) / Math.max(1, lats.length),
        ];
      map = new m.Map({
        container: el,
        style: p.style_url ?? "https://demotiles.maplibre.org/style.json",
        center,
        zoom: p.zoom ?? 2.5,
        attributionControl: false,
      });
      const instance = map as unknown as InstanceType<typeof m.Map>;
      (p.points ?? []).forEach((pt) => {
        const marker = new m.Marker({ color: "#0f172a" }).setLngLat([pt.lon, pt.lat]);
        if (pt.label) {
          marker.setPopup(new m.Popup({ closeButton: false }).setText(pt.label));
        }
        marker.addTo(instance);
      });
    });
    return () => {
      disposed = true;
      if (map) map.remove();
    };
  }, [p._placeholder, p.points, p.style_url, p.center, p.zoom]);

  if (p._placeholder || !p.points || p.points.length === 0) {
    return (
      <CardShell title={p.title || "Map"} subtitle={<Pill tone="slate">No geo data</Pill>}>
        <p className="text-xs text-slate-500">Provide `points` with lat/lon to render the map.</p>
      </CardShell>
    );
  }
  return (
    <CardShell title={p.title || "Map"} subtitle={`${p.points.length} points`}>
      <div ref={ref} className="h-64 w-full overflow-hidden rounded" data-testid="map-card" />
    </CardShell>
  );
}
