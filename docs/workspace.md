# Workspace (A2UI-native)

The 3-panel React UI's right panel is five A2UI surface tabs: **Wiki / Library / Notes / Hypotheses / Briefs**. The right panel renders **exclusively** through the A2UI protocol against one shared catalog — no self-fetching components.

## Data-binding architecture + delta substrate

Each canonical tab (Wiki, Library, Notes, Hypotheses) is **server-built and data-bound**. The backend surface builders in `packages/aleph-a2ui/.../components/surfaces.py` expose a `<tab>_v09(project_id, session)` builder that:

1. loads its rows through the existing service layer,
2. emits `createSurface` + `updateComponents` (structure) once, and
3. populates a `dataModel` whose shape is the tab's typed schema (`surfaces_schema.py`, mirrored in the FE catalog contract).

Per-tab data-model shapes: `wiki = {pages: [...], open: {page_id, revision:{body_md}, claims[], citations[], wikilinks_out[]} | null}`; `library = {sources, artifacts}`; `notes = {notes: [...]}`; `hypotheses = {items, ach | null}`. Briefs is agent-composed (below).

**Delta emission.** The surface stream wakes on the `ChangeBroker` (Postgres `pg_notify('aleph_changes')` → `NotifyListener`). On wake the tab recomputes its data model and emits `diff_data_model(prev, next)` → `updateDataModel` messages; structural diffs still re-send `updateComponents`. There are **no polling intervals in the right panel**; a 10s `sub.wait` fallback stays as a self-heal.

**Reconnect / resume / ordering.** Every SSE message carries a monotonic per-surface `seq` (`id:` field + payload). On reconnect the client sends `Last-Event-ID`; the server replays from a bounded per-connection ring buffer, or re-sends a full snapshot with a fresh baseline `seq` when the id is out of range. Messages apply in `seq` order; out-of-order/duplicate `seq` is dropped. Three integration tests prove the substrate is load-bearing: resume-applies-missed-deltas, gap→full-resync, and seq-ordering.

**No-self-fetch enforcement.** `scripts/check-no-self-fetch.sh` (CI) greps `apps/web/src/a2ui/components` for `useQuery`/`useMutation`/`refetchInterval`/`EventSource`/`fetch(`/`api.get|post|put|delete` and fails on any hit; the allowlist is empty. Mutating actions call `onAction(name, params)`, routed through the ledger-audited action router (`emitAction` in `surface-context`) — never react-query.

## Reader / editor tier

- **`WikiPageCard`** — the rich markdown reader, receiving `{body_md, claims, citations, wikilinks_out, page_meta}` as bound props. It reuses the `WikiBodyMarkdown` tokenizer, upgraded so wikilinks emit `open_page` **A2UI actions**, `[cN]` markers open a citation popover (resolved `Citation` → source title + URL), and claims render **confidence badges** + a **freshness badge** (populated by the WP-6 trust layer). No `dangerouslySetInnerHTML`.
- **`NoteEditorCard`** — a bound-props note editor (title + `body_md`), debounced `edit_note` action through the router. Markdown only.
- **Deterministic HTML compiler + `HtmlDocCard`.** A server-side, **non-LLM** compiler (`packages/aleph-wiki/.../html_compiler.py`) turns a wiki page (`body_md` + claims + optional `infobox_jsonb`) into a single self-contained styled HTML document (inline CSS, no scripts, no external refs). It is deterministic (byte-identical output; property-tested). The output is stored as a rendered artifact and shown by `HtmlDocCard` in a **sandboxed iframe** referencing it by the streaming-route URI. **Markdown is the only wiki write-format** — there is no `body_html` write path; the compiler reads markdown, never writes it.

## The sandbox viz pipeline

- **`aleph-code-runner` is a separate, credential-less compose service** built from `apps/code-runner/Dockerfile`: Python + a fixed audited scientific stack (matplotlib, numpy, pandas, vega/altair) and nothing else. Isolation: an `internal: true` network reaching **only** a dedicated `code-runner-redis` (no internet, no Postgres, no aleph-api), `cap_drop: [ALL]`, `read_only: true` rootfs with a small tmpfs, `pids_limit`, tight `mem_limit`/`memswap_limit`, `no-new-privileges`, non-root user, and **no** `DATABASE_URL`/`ALEPH_S3_*`/`LITELLM_*`/`ALEPH_AGENT_TOKEN_SECRET` env and no asset bind mount. It consumes `run_code_job(code, output_kind, timeout_s)` off a dedicated `code_runner` queue.
- **Execution contract.** A job carries agent-written Python + a declared output kind (`png` | `svg` | `vega` | `html`). The runner executes it in a subprocess (wall-clock timeout, the caps above), captures the single declared output from a fixed scratch path, and returns `{ok, bytes_b64 | spec_json, mime, error?}`. Code attempting network/file/DB access fails closed. **No `exec()` of agent code runs in any credentialed process.**
- **Versioned artifacts.** A privileged step in `aleph-workers` (which *does* hold the store) persists the returned bytes as a `RenderedAsset`/`ArtifactVersion` with a `producing_code` field (in `lineage_jsonb.producing_code` + its sha), a `sha256` checksum, and `builder_agent_run_id` lineage. New viz artifact kinds (`image`, `chart`, `html_frame`) are in the `artifact_kind` allowlist; an unimplemented kind 400s.
- **Catalog cards reference artifacts by URI.** `ImageCard` (`<img src=streaming-route>`), `ChartCard` (Vega-spec artifact URI or inline bound spec — no fetch), and `HtmlFrameCard` (interactive HTML **only** inside a `sandbox` iframe: scripts allowed, **no `allow-same-origin`, no network**). The renderer refuses to mount an `HtmlFrameCard`/`HtmlDocCard` whose `src` is not the asset streaming route — amended rule 8 enforced at the renderer.

## Agent integration (eyes + hands)

- **Shared-state readables.** CopilotKit shared state exposes `{active_tab, open_page_id, open_page_title, selection}`. "Summarize this page" works with no page named because `open_page_id` is in shared state.
- **Frontend actions (agent → workspace).** Four tools bridged to the action router: `open_page(page_id|slug)`, `focus_tab(tab)`, `pin_to_brief(card)`, `highlight_claim(claim_id)`. Each dispatches through the ledger-audited router (`action_router.py` → `CardAction` row + ledger event).
- **Agent composition verbs.** `pin_to_brief`, `compose_dossier(title, card_ids|page_ids)` (composes a derived, read-only Briefs card grouping others), and `spotlight(card_id)` (orders one Briefs card first with a spotlight flag). All three go through the router and are ledgered.

## The catalog roster

The catalog holds **20 components** (surfaces + cards). Deleted (appear nowhere): **`MapCard`, `GraphCard`, `NotebookCellCard`**.

| Card | Decision | Producer |
|------|----------|----------|
| ClaimCard | keep | `surfaces.py` wiki claims |
| ApprovalCard | keep | `surfaces.py` synthesis/merge/refresh proposals |
| FindingCard | keep | `surfaces.py` review findings |
| HypothesisCard | keep | Hypotheses builder |
| ArtifactCard | keep | Library builder |
| SourceCard | rebuilt (bound preview) | Library builder |
| ChartCard | rebuilt (artifact-URI / inline spec) | `viz_builder` + code_runner |
| WikiPageCard | new | wiki builder |
| NoteEditorCard | new | notes builder |
| HtmlDocCard | new | deterministic HTML compiler |
| ImageCard | new | code_runner |
| HtmlFrameCard | new | code_runner |
| TableCard | rebuilt (bound rows) | dataset/agent producer |
| FormCard | keep (agent-emit) | agent `render_a2ui` |
| DiffCard | keep (real diff) | ApprovalCard.diff_card_id |
| Wiki/Library/Notes/Hypotheses surfaces | rebuilt data-bound | `surfaces.py` `*_v09` builders |
| BriefsSurface | keep (agent-composed workbench) | pinned/composed/spotlighted cards |

The **producer/renderer sweep** `scripts/check-catalog-roster.sh` (CI) asserts every catalog name has both a renderer and a producer, that `catalog.py` ⟷ `catalog.ts` agree, and that the deleted names appear nowhere.

## Security posture

Amended rule 8 is enforced two ways: no agent code executes in a credentialed process (only the network-less `code_runner`), and interactive artifacts render only in `sandbox` iframes whose `src` must be the asset streaming route. A full `code_runner` escape yields only CPU/mem within the cgroup caps and the agent's own code. Frontend actions remain ledger-audited through the one action router. Markdown stays the only wiki write-format.
