/**
 * The chat path carries a credential — and carries it in the shape the library
 * actually reads.
 *
 * The provider used to pass `runtimeUrl`, `renderActivityMessages` and
 * `openGenerativeUI` and nothing else, so the Node bridge called the API
 * anonymously: the API saw the BRIDGE rather than the person, while every other
 * route in the app had been authenticated since the `/copilotkit` exemption was
 * removed.
 *
 * The second assertion is the one a grep cannot make. CopilotKit types `headers`
 * as `Record<string, string> | Headers` — an OBJECT. A function is accepted by
 * JavaScript without complaint and serialised to nothing, so `grep -c headers`
 * stays at 1 and the request goes out bare. WS-D3 wanted this test and had to
 * ship the grep instead, because apps/web had no runner.
 */
import { render, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

interface ProviderProps {
  runtimeUrl?: string;
  headers?: unknown;
  children?: ReactNode;
}

const seen: ProviderProps[] = [];

vi.mock("@copilotkit/react-core/v2", () => ({
  CopilotKitProvider: (props: ProviderProps) => {
    seen.push(props);
    return <div data-testid="copilot-provider">{props.children}</div>;
  },
  createA2UIMessageRenderer: () => ({ id: "a2ui-renderer" }),
  a2uiDefaultTheme: {},
}));

// The real catalog pulls in all 21 card renderers (vega-embed among them) to
// prove nothing about the credential. Stubbed so this file tests one thing.
vi.mock("@/a2ui/aleph-catalog-v09", () => ({ buildAlephCatalog: () => ({ id: "aleph://v1" }) }));

import { AlephCopilotProvider } from "@/lib/copilot";

describe("AlephCopilotProvider", () => {
  it("mounts CopilotKitProvider with the caller's bearer token", async () => {
    render(
      <AlephCopilotProvider>
        <span>child</span>
      </AlephCopilotProvider>,
    );
    await waitFor(() => {
      const latest = seen.at(-1);
      expect(latest?.headers).toEqual({ Authorization: "Bearer local-dev" });
    });
  });

  it("passes headers as an object, not a function", () => {
    // A function here is the silent-failure shape: JS accepts it, the transport
    // serialises it to nothing, and the request goes out with no Authorization
    // while the source still reads `headers={…}`.
    const latest = seen.at(-1);
    expect(typeof latest?.headers).toBe("object");
    expect(typeof latest?.headers).not.toBe("function");
  });

  it("points at the configured runtime rather than a compiled-in guess", () => {
    expect(seen.at(-1)?.runtimeUrl).toBe("http://localhost:4000/api/copilotkit");
  });

  it("renders its children", () => {
    const view = render(
      <AlephCopilotProvider>
        <span data-testid="inner">child</span>
      </AlephCopilotProvider>,
    );
    expect(view.getByTestId("inner").textContent).toBe("child");
  });
});
