# Assistant agent

A LangGraph turn workflow that drives the wiki-first retrieval router
and finalizes one assistant message per user turn.

## Nodes

1. **`budget_gate`** — read `Budget.spent_usd` and the project's cap.
   If at hard cap, mark the message `status="budget_blocked"` with a
   clear body, append a ledger event, end the workflow without an LLM
   call.
2. **`query_rewrite`** — short pronoun-substitution rewrite that pulls
   `[[wikilink]]` context out of the prior assistant message when the
   user query is a deictic shortcut. Inc 2 keeps this deterministic;
   the spec's note allows for a small LLM-rewrite follow-on.
3. **`retrieve`** — calls `WikiFirstRetrievalRouter.retrieve`. The
   router does FTS → LLM page-select → 1-hop expansion → composer, then
   loops once into descent if `descent_requests` came back.
4. **`finalize`** — persist body, retrieval snapshot, latency, and
   status; append `assistant.message.complete` (or failure variant)
   ledger event.

## State

`AssistantTurnState` is a `TypedDict` containing the turn's IDs,
prior messages, profile bindings, and progressive results. The
`_active_ctx` module-level singleton carries non-serializable
dependencies (session_maker, litellm, principal) because LangGraph
state is JSON-shaped.

## Failure handling

- Composer error / RuntimeError → finalize with `status="failed"`,
  `error_text` populated, ledger event `assistant.message.failed`.
- Budget exceeded → finalize with `status="budget_blocked"`, no LLM
  call, clear body explaining who can raise the cap.

Every LLM call inside the workflow goes through `LiteLLMClient.chat`
or `.embed`, so each call writes `ModelCall` + `CostLedgerEvent`. The
roll-up updates `budgets.spent_usd` via the existing trigger.

## Ledger taxonomy

- `assistant.session.create` / `assistant.session.rename`
- `assistant.thread.fork`
- `assistant.message.user_posted`
- `assistant.message.complete` / `assistant.message.failed` / `assistant.message.budget_blocked`
