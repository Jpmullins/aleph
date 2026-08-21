# Attribution

Where borrowed designs and code came from, and under what license. Aleph **reimplements**
ideas rather than vendoring code; anything actually ported carries a `NOTICE` beside it
(`packages/aleph-belief/NOTICE`, `skills/literature-review/NOTICE`).

| source | license | what was taken |
|---|---|---|
| [graphify](https://github.com/rhanka/graphify) | MIT | the patch contract (propose → validate → apply), trust tiers, verbatim grounding, deterministic entity matching, `citationKey` union-not-clobber merge |
| claude-science **skills** | Apache-2.0 | usable verbatim with attribution — the `literature-review` skill in particular |
| claude-science **harness** | proprietary | **design reference only — no code copied.** It is a reconstruction of a compiled binary |
| [cordis](https://github.com/cordiverse/cordis) + [its paper](https://github.com/cordiverse/paper) | MIT | the spatiotemporal composability model — revertible effects, reactive coeffects, scoped context |
| [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | MIT | capability seams; honest diagnostic misses over degraded fallbacks |

Reference implementations under `~/Documents/code/inspiration/` — cordis, deepseek-harness,
prime-agent, opencode, hermes-agent — are **read, not depended on**. None is a runtime
dependency. Ideas mined from them are reimplemented; any ported code gets a `NOTICE`.
