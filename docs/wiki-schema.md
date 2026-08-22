# The wiki schema

Governance for Aleph's wiki, ported from the hermes-agent `llm-wiki` skill — the
harness that built `~/wiki/ai-research`. This document describes what is built.

## Why governance at all

A wiki degrades in ways no single write notices. One page links to a title
nobody wrote. One page nothing links to. One tag invented on the fly. One
contradiction resolved by whichever ingest ran last. Each is harmless; the
accumulation is a corpus that answers questions wrongly while every individual
operation reports success.

The hermes skill answers this with a `SCHEMA.md` the agent must read before it
writes. Aleph stores the same thing **as data**, because an agent can ignore a
document: `WikiSchema.validate_page` returns every violation a page has as a
value a caller can act on mechanically.

**It does not run on the write path.** This document claimed it did, and it did
not: `validate_page`'s only non-test caller is `aleph_wiki.lint`, which reports
and never repairs. So an undeclared tag is committed and then discovered in a
lint six weeks later — precisely the gap the sentence above used to claim was
closed. Closing it for real means calling `validate_page` from
`WikiService.commit_revision` and deciding what a violation does there: reject
the commit, or accept it and flag the page. That decision has not been made, and
until it is, the schema is a governance *report*, not a gate.

The vocabulary mirrors the hermes frontmatter field-for-field (`type`,
`category`, `tags`, `related`, `sources`, `confidence`, `contested`,
`contradictions`), so a page written here opens in Obsidian and a vault authored
in Obsidian imports without translation. Verified against the real vault: 560
files, 0 round-trip failures.

## The shape

| Layer | Where | What |
|---|---|---|
| Schema | `wiki_schemas` table, one row per project | domain, categories, tag taxonomy, thresholds |
| Page fields | `wiki_pages` columns | `category`, `page_type`, `tags`, `related`, `confidence`, `contested`, `contradictions` |
| Frontmatter | `aleph_wiki.frontmatter` | the only code that moves between YAML and columns |
| Validation | `aleph_wiki.schema` | returns every violation, not the first — called by the lint, not by the write path |
| Health | `aleph_wiki.lint` | 16 read-only checks, severity-ordered |
| Navigation | `aleph_wiki.navigation` | hubs and index, derived rather than hand-maintained |

The fields are columns rather than markdown because each is something the system
must filter, group or lint on, and parsing 251 bodies to answer "which pages are
contested" is not a query.

**`page_type` is not `page_kind`.** `page_kind` records how Aleph produced the
page (source ingest, stub minted from a link, synthesis run). `page_type` records
what kind of knowledge it holds (concept, entity, comparison, query, hub). A page
is routinely `page_kind="source"` and `page_type="entity"` at once.

## The page lifecycle

Four statuses. **Two of them are queues for a person, and they are not the same
queue** — this is the distinction the original design lacked.

| Status | Meaning | Is it work? |
|---|---|---|
| `stub` | A red link. Something linked to a title nobody wrote. | No. Nobody proposed it. |
| `planned` | Cited enough to earn a page. The hermes 🚧 state. | Yes — a queue for **writing**. |
| `draft` | Has content. | Yes — a queue for **review**. |
| `approved` | Reviewed and accepted. | No. |

Filing stubs as `draft` put 235 empty placeholders in front of an approver
alongside 15 real pages. An approval gesture performed 235 times cannot mean "I
read this and agree"; it can only mean "make the banner go away", which makes the
15 real reviews worthless too.

Promotion moves a stub to `planned`, never to `draft`. `is_stub` stays true until
something writes a body — the page earned attention, not content.

## Thresholds

```
min_outbound_links       3     a page linking nowhere is unreachable by walking
page_split_lines         200   past this it is several pages
stub_promotion_mentions  5     distinct citing pages before a title earns a page
```

**The promotion threshold is 5, not the hermes 2.** hermes uses 2 because a human
types `[[link]]` deliberately, so two mentions is two acts of judgement. Aleph
extracts links mechanically from compiled prose, where a mention is free.
Measured on the real corpus, a threshold of 2 selects **477 of 600** linked
stubs — that counts how common a phrase is, not whether a topic deserves a page.
5 is where the curve flattens. It is per-project: a hand-curated vault imported
into Aleph should set it back to 2.

## Deriving a schema

Aleph ships a default describing AI/ML research, because the first plugin suite
is research. On any other project it is wrong, and **a wrong taxonomy is worse
than none**: it gives every page a plausible-looking home, so nothing reports a
problem while the categories quietly stop meaning anything.

```
POST /v1/projects/{id}/wiki/schema/propose   read-only; returns a proposal + diff
PUT  /v1/projects/{id}/wiki/schema           accepts it (editor, one ledger event)
POST /v1/projects/{id}/wiki/classify         files pages into the schema in force
```

On the live corpus the proposal came back as *"Storage systems and database
architecture — distributed storage, logging mechanisms, SSD optimization, formal
verification"* with ten categories that actually partition it. Classification
then filed 234 of 251 pages in 40 seconds with zero categories invented outside
the schema.

The 17 it declined are the point — Digamma Function, Harmonic Sum, Extreme
Programming, Refactoring. None belong in a storage taxonomy, and the prompt tells
the model to leave a page unfiled rather than force it. **A page filed where it
does not belong is worse than one left unfiled, because nothing will ever report
it as wrong.** The lint reports the unfiled ones as `uncategorised`.

The schema is the authority over the model: a proposed category outside it is
rejected, not adopted, or the taxonomy would grow by whatever a model emitted.

`RESERVED_TAGS` (`hub`, `index`) are valid in every wiki regardless of domain —
they describe a page's structure, not its subject. Without them, generated hubs
violate the schema the moment a project derives its own taxonomy.

## Lint

`GET /v1/projects/{id}/wiki/lint`, or `wiki_lint_report` from the agent.
Read-only — it reports, it never repairs, because repair is a write and writes go
through the service so they land in the ledger.

Severity order is `broken` → `structure` → `quality` → `style`. The report is
read top-down by someone with ten minutes, so it puts what breaks navigation
first and style last.

Checks: broken wikilinks · orphans · uncategorised · unknown category · schema
violations · frontmatter/column drift · contested · low confidence · unjudged ·
stale · tags outside the taxonomy · duplicate slugs · unresolved `related` ·
contradictions pointing nowhere · near-duplicates · the writing backlog.

Stubs are counted and skipped, not reported: every check would fire on every
stub and the report would be 94% noise — the same failure the review queue had.

**The lint resolves links by exactly the rules the repair uses** (title,
case-insensitive title, then slug). They used to differ, so `[[Source:S0002]]`
read as broken forever while the repair resolved it by slug on every run — a
report naming a problem that repairing could not remove.

## Links

`POST /v1/projects/{id}/wiki/links/resolve` (`?dry_run=true` to preview).

Resolution order: exact title → case-insensitive title → slug. Slug resolution is
the documented vault behaviour, not a workaround: slugs are globally unique so
`[[slug]]` resolves wherever the page lives, which is how Obsidian's
shortest-path linking works.

The pre-existing repair went only through the legacy alias table, so a link
matching a page's title *exactly* did not resolve. On the real corpus this left
**567 links unresolved that had targets** — 376 by slug, 191 by title. Every
source page was unreachable through its own `[[Source:SNNNN]]` links.

A link is never pointed at its own page. A self-link is not navigation, and it
would make the page its own inbound link — which is precisely what the orphan
check reads.

## Navigation

Hubs and the index are **derived**, not hand-maintained. The hermes skill keeps
`_hub.md` and `index.md` as files somebody must remember to update, and names the
decay that follows when they forget. Here both are queries, so a page that exists
is listed by construction.

`POST /wiki/hubs/sync` writes hubs as real pages so `[[logging-recovery-hub]]`
resolves like any other wikilink and an exported vault opens in Obsidian with its
navigation intact. Idempotent by body hash — without that, a scheduled
regeneration would append a revision per category per run to an immutable
append-only table, and freshness would report hubs as the most recently edited
pages in the wiki.

`GET /wiki/index` is sectioned by category with an **uncategorised tail**. The
hermes index has no place for a page with no section, so such a page is simply
absent — present in the vault, missing from the one document meant to list
everything. Showing it makes the gap visible.

## Adding to this

- **New tag** → add it to the schema first (`PUT /wiki/schema`), then use it. The
  write path rejects an undeclared tag; that is the point of a controlled
  vocabulary.
- **New lint check** → return a `Finding` with a `fix` that names the action, and
  write the test in both directions. A checker that only ever fires is
  indistinguishable from one hardcoded to fire.
- **New page field** → column on `WikiPage`, field on `Frontmatter`, entry in
  `_ORDER`, and a rule in `validate_page`. All four, or the field is written by
  one path and read by another.
- **New surface prop** → declare it in the component's zod schema in
  `apps/web/src/a2ui/aleph-catalog-v09.tsx`. `scripts/check-surface-bindings.sh`
  fails otherwise.
