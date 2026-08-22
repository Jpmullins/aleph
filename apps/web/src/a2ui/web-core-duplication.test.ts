/**
 * Two `@a2ui/web_core` versions resolve, and this is the note that accepts them.
 *
 * WS-UI-3 c5 asks for exactly one, "or, if two are accepted, a committed note
 * states why and the assertion pins exactly those two". This is both halves.
 *
 * WHY TWO.
 * `apps/web` declares `@a2ui/web_core: ^0.10` and gets 0.10.0.
 * `@copilotkit/a2ui-renderer@1.58.0` depends on `"@a2ui/web_core": "0.9.0"` —
 * an EXACT version, not a range, so pnpm cannot dedupe it and no floor in
 * `pnpm-workspace.yaml` can be satisfied by both. The only way to one version
 * is a pnpm `overrides` entry forcing the renderer onto 0.10.0.
 *
 * WHY IT MATTERS.
 * `lib/copilot.tsx` builds Aleph's chat catalog with 0.10.0's `Catalog` and
 * `CommonSchemas` and hands it to `createA2UIMessageRenderer`, which is the
 * renderer's — 0.9.0's — `MessageProcessor` and `GenericBinder`. A component
 * registered against one catalog implementation and rendered by the other is
 * the concern, and it is live on every agent-emitted card in chat.
 *
 * WHY IT IS SAFE TODAY, AS MEASURED HERE RATHER THAN ASSERTED.
 * 0.10.0's `v0_9` API is a strict superset of 0.9.0's: the same `CommonSchemas`
 * keys, the same `Catalog` and `MessageProcessor` prototypes, five added
 * exports and no removals. Neither version gates on `instanceof Catalog`
 * anywhere — the only `instanceof` in either is over zod and preact signals.
 * So the object one side builds is the shape the other side reads.
 *
 * That is a property of the two versions installed, not a law, which is why it
 * is a test and not a paragraph. Bump either side into a divergence and this
 * goes red, before the chat surface silently stops rendering cards.
 */
import { createRequire } from "node:module";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

/** The pair this repository has looked at and accepted. */
const ACCEPTED = {
  /** What `apps/web` imports — `aleph-catalog-v09.tsx`, `SurfaceStreamProvider`. */
  app: "0.10.0",
  /** What `@copilotkit/a2ui-renderer@1.58.0` pins, and chat therefore renders with. */
  renderer: "0.9.0",
} as const;

const require_ = createRequire(import.meta.url);

/**
 * The `@a2ui/web_core` entry point a given package will actually load.
 *
 * The version comes from the package's own `package.json`, read off disk by
 * walking up from the resolved entry — `require.resolve` cannot reach it,
 * because `@a2ui/web_core` does not export the `./package.json` subpath. Not
 * from the `.pnpm/@a2ui+web_core@x.y.z/` path segment either: that is the store
 * layout, and a different install mode would make this assert about pnpm.
 */
function resolvedCore(from: NodeRequire): { path: string; version: string } {
  const path = from.resolve("@a2ui/web_core/v0_9");
  const root = path.slice(0, path.indexOf("/web_core/") + "/web_core/".length);
  const version = String(
    (JSON.parse(readFileSync(`${root}package.json`, "utf8")) as { version: string }).version,
  );
  return { path, version };
}

const app = resolvedCore(require_);
const renderer = resolvedCore(
  createRequire(
    createRequire(require_.resolve("@copilotkit/react-core/package.json")).resolve(
      "@copilotkit/a2ui-renderer/package.json",
    ),
  ),
);

interface WebCoreModule {
  CommonSchemas: Record<string, unknown>;
  Catalog: new (id: string, impls: unknown[], funcs: unknown[]) => { id: string };
  MessageProcessor: new (...args: never[]) => unknown;
}

async function load(path: string): Promise<WebCoreModule> {
  return (await import(/* @vite-ignore */ path)) as WebCoreModule;
}

function protoNames(ctor: new (...args: never[]) => unknown): string[] {
  return Object.getOwnPropertyNames(ctor.prototype).sort();
}

/** Every `.js` under a directory tree, read. */
function jsSources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...jsSources(full));
    else if (entry.name.endsWith(".js")) out.push(readFileSync(full, "utf8"));
  }
  return out;
}

/**
 * The repo root, derived from where `@a2ui/web_core` resolved to.
 *
 * `import.meta.url` is a Vite module URL under vitest, not a `file:` one, so
 * `new URL("../../../../pnpm-lock.yaml", import.meta.url)` throws. The install
 * root is a fact this test already has in hand.
 */
const REPO_ROOT = app.path.slice(0, app.path.indexOf("/node_modules/"));

describe("@a2ui/web_core duplication", () => {
  it("resolves exactly the two versions this repository has accepted", () => {
    // Named individually, not counted. "two versions" would still pass after
    // somebody bumped one of them into an untested pair, which is the change
    // this is here to notice.
    expect({ app: app.version, renderer: renderer.version }).toEqual(ACCEPTED);
  });

  it("finds no third copy anywhere in the lockfile", () => {
    // The lockfile, not `node_modules`: a version can be resolved for a
    // workspace member nobody has installed locally, and this file has to fail
    // in CI for a dependency `apps/web` alone would never pull.
    const lock = readFileSync(join(REPO_ROOT, "pnpm-lock.yaml"), "utf8");
    const found = [...lock.matchAll(/@a2ui\/web_core@(\d+\.\d+\.\d+)/g)].map((m) => m[1]);
    expect([...new Set(found)].sort()).toEqual(["0.10.0", "0.9.0"]);
  });

  it("offers the renderer every prop schema the app's catalog is built from", async () => {
    // `CommonSchemas.DynamicString`/`.Action` are how every Aleph card declares
    // a bindable prop. The binder that resolves them is the renderer's copy.
    const [a, b] = await Promise.all([load(app.path), load(renderer.path)]);
    expect(Object.keys(a.CommonSchemas).sort()).toEqual(Object.keys(b.CommonSchemas).sort());
    expect(Object.keys(a.CommonSchemas).length).toBeGreaterThan(10);
  });

  it("keeps the Catalog and MessageProcessor shapes the two sides pass between them", async () => {
    const [a, b] = await Promise.all([load(app.path), load(renderer.path)]);
    // The app builds this object; the renderer reads it. A method present on
    // one prototype and absent on the other is the crash.
    expect(protoNames(a.Catalog)).toEqual(protoNames(b.Catalog));
    expect(protoNames(a.MessageProcessor)).toEqual(protoNames(b.MessageProcessor));
  });

  it("has no instanceof gate that a cross-version object would fail", () => {
    // The failure mode this whole file is about: `if (!(catalog instanceof
    // Catalog)) throw`. Two copies of a class are two identities, so such a
    // gate turns a working catalog into a runtime error at the moment the two
    // sides meet. Neither version has one; if one appears, accepting two stops
    // being an option and the pnpm override becomes mandatory.
    for (const entry of [app.path, renderer.path]) {
      const gates = jsSources(dirname(entry)).filter((text) =>
        /instanceof\s+Catalog/.test(text),
      );
      expect(gates).toEqual([]);
    }
  });
});
