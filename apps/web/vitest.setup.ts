/**
 * Runs before every test file.
 *
 * React Testing Library registers its own `afterEach(cleanup)` only when the
 * test globals are installed, and this project runs with `globals: false` on
 * purpose. Without the explicit unmount below, every rendered tree stays in the
 * jsdom document for the rest of the file: `getByTestId` then finds two nodes
 * and fails with "found multiple elements", which reads as a broken component
 * rather than as leaked state from the previous test.
 */
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
