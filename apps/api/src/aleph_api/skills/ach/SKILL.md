---
name: ach
description: Analysis of Competing Hypotheses (Heuer's method) — enumerate hypotheses, list evidence, score each evidence item for consistency against every hypothesis, and prefer the hypothesis with the fewest disconfirming items. How to drive the analyst subagent and the Hypotheses tab matrix.
---

# Analysis of Competing Hypotheses (ACH)

Heuer's structured method for weighing competing explanations. Use it whenever
the analyst is choosing between rival interpretations of the same evidence.

## The method

1. **Enumerate hypotheses** — list the full set of mutually exclusive,
   plausible explanations up front. Include the unlikely ones; missing a
   hypothesis is the most common failure.
2. **List the evidence** — gather every relevant item of evidence and argument,
   including absence-of-evidence where it is diagnostic.
3. **Score consistency** — for each evidence item, mark whether it is
   consistent (C), inconsistent (I), or not applicable (N/A) with *each*
   hypothesis. Work across rows (evidence), not down columns.
4. **Focus on disconfirmation** — you cannot prove a hypothesis, only fail to
   disprove it. **Prefer the hypothesis with the fewest inconsistent
   (disconfirming) items**, not the one with the most consistent items.
5. **Identify diagnostic evidence** — evidence consistent with every hypothesis
   has no diagnostic value; the items that discriminate between hypotheses
   matter most.
6. **Report sensitivity** — note which few evidence items, if wrong, would flip
   the conclusion.

## How to drive it in Aleph

- Delegate enumeration, creation, and evidence-weighing to the `analyst`
  subagent via the `task` tool. It lists/creates hypotheses and attaches
  evidence with a stance (supports / refutes), and returns a HypothesisCard
  render instruction — render it.
- Confirm the exact hypothesis statement with the analyst before creating one.
- Each hypothesis carries a confidence that updates as supporting/refuting
  evidence accrues.
- The full consistency grid lives in the **Hypotheses tab** as the ACH matrix
  (evidence rows × hypothesis columns); point the analyst there to see and edit
  the scoring.
