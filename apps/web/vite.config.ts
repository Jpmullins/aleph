import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * In a container the source tree is a bind mount, and on macOS (Docker Desktop,
 * colima, OrbStack) filesystem events do not cross the VM boundary. Vite never
 * hears that a file changed, so it keeps serving the module it transformed at
 * boot — the edit is on disk, is visible inside the container, and still does
 * not reach the browser. Nothing reports an error: HMR stays connected and a
 * hard reload returns the same stale module, which reads as "my change had no
 * effect" rather than "the change was never compiled".
 *
 * Polling is the only reliable watcher across that boundary. It costs CPU, so
 * it is on only where it is needed: the container sets ALEPH_IN_CONTAINER, and
 * a developer running `pnpm dev` on the host keeps native events.
 */
const inContainer = process.env.ALEPH_IN_CONTAINER === "1";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    watch: inContainer ? { usePolling: true, interval: 300 } : undefined,
  },
  resolve: {
    alias: {
      "@": new URL("./src", import.meta.url).pathname,
    },
  },
  build: {
    target: "es2022",
    sourcemap: true,
  },
});
