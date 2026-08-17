# AGENTS.md

Instructions for coding agents working in this repository.

**Read [`CLAUDE.md`](CLAUDE.md) first — it is the authoritative guide.** This file exists so agents
that look for `AGENTS.md` by convention find the right place; it does not duplicate that content,
because two drifting copies of the same rules is how the previous doc set became untrustworthy.

Three things worth repeating here, because they are the ones most often violated:

1. **Verify before you write it down.** Do not add a claim to a doc, a docstring, or a comment
   unless you have checked it against the code. The prior doc set asserted invariants that were
   false and CI checks that did not exist.

2. **Ship a consumer with every producer.** The dominant defect in this codebase is a column,
   table, or service written correctly and read by nothing — invisible to tests, because fixtures
   hand-build the rows the real pipeline never writes. A contract with no caller is not progress.

3. **`packages/aleph-wiki` is legacy under removal.** Do not extend it or add tests to it. Migrate
   callers off it. See `docs/decisions.md`.
