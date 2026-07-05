---
description: Reconstruct app intent, then validate whether the codebase achieves it with executable checks
---

You are auditing this full-stack codebase in two phases. Do not skip to conclusions. Produce artifacts, not just prose.

## Phase 1: Intent reconstruction

Read the codebase thoroughly. Determine:
- What is this app supposed to do? (the intended product)
- What does it actually do? (observed behavior from code, not docs)
- Where are the gaps, unwired pieces, dead code, half-built features, and mistakes?

Write your findings to `audit/intent.md`. Then write a machine-checkable claims file to `audit/claims.yaml` with this schema, one entry per intended capability:

```yaml
- id: checkout-completes
  intent: "A user can add an item to cart and complete checkout, ending on an order-confirmation state."
  evidence:            # where in the code this is implied
    - src/routes/checkout.ts
    - src/pages/Cart.tsx
  check_type: e2e      # one of: e2e | http | route | dataflow | build | static
  status: unverified   # you will not set pass/fail here; the harness does
  wired: true|false    # your judgment: is this feature actually connected end to end?
  notes: "..."
```

Rules for claims:
- Every user-facing capability the app appears to intend gets a claim, even if you believe it is broken. A broken feature is a FAILING check, not an omitted one.
- Mark `wired: false` for anything with a UI entry point but no working backend, or a backend endpoint no UI calls. These are the gaps that npm test never catches.
- Do not invent capabilities. Each claim needs `evidence` pointing at real files.

## Phase 2: Validation harness

For each claim, generate an executable check under `audit/checks/`, keyed by claim `id`. Match `check_type`:

- **e2e**: a Playwright spec that drives the real UI to the claimed end state. It must assert on observable outcomes (URL, DOM text, network response), not that a component rendered.
- **http**: a script that hits the running API and asserts status + response shape.
- **route**: assert every route in the router resolves to a real handler/component and is reachable from a UI entry point (link, button, redirect). Flag orphan routes.
- **dataflow**: trace one claimed path (e.g. form submit -> API -> DB write -> read-back) and assert the value survives the round trip.
- **build/static**: typecheck, lint, dead-export detection, unresolved-import detection.

Then generate `audit/run.sh` that:
1. Starts the app (respect existing scripts in package.json / Makefile; do not fabricate).
2. Runs every check.
3. Writes `audit/scorecard.json`: for each claim id, `{pass, fail, error, skipped}` plus a one-line reason.
4. Prints a summary table and an overall "intent coverage" ratio: claims passing / total claims.

Do not mark a claim as passing unless its check actually ran and asserted the intended outcome. A check that errors because the feature is unbuilt is a FAIL, and that is the signal we want.

## Output

End with:
1. The scorecard table.
2. A ranked list of the largest intent gaps (unwired pieces, dead ends, broken flows) with file paths.
3. What you could not verify and why.

