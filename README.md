# Aleph

A **general-purpose, self-improving multi-agent harness**.

An agent that authors plugins for itself and activates or deactivates them as needed — on a kernel
whose composability model makes that safe, with guardrails that stop it removing load-bearing
capability. Every capability, including the agent loop, is a plugin.

Its first plugin suite is **research**: hand a project sources or a question and it reads real
literature — verifying DOIs, walking citation graphs forward and backward, checking for retractions —
and accumulates a durable, cited body of knowledge you can query, correct, and export.

That knowledge layer is a **web of belief**: claims are first-class and evidence-anchored, confidence
is *derived* from what supports them rather than asserted by a model, and adding a source or learning
of a retraction propagates through the graph. Reports and HTML artifacts are rendered from that layer;
they are not the layer.

> **Status:** Aleph is mid-transition on two axes. The LLM-maintained wiki is being replaced by the
> Claim Spine, and the whole system is being rebuilt on an own-implemented composability kernel.
> The kernel language and structure are an **open decision**. See
> [`docs/decisions.md`](docs/decisions.md) for the reasoning, [`docs/belief-engine.md`](docs/belief-engine.md)
> for the knowledge design. `packages/aleph-wiki` is legacy under removal.

## Quick start

```bash
cp deploy/compose/.env.example deploy/compose/.env   # set INSIGHTS_LITELLM_API_KEY
./scripts/bootstrap-local.sh
```

- Web UI — http://localhost:5173
- API — http://localhost:8000
- Copilot runtime (chat bridge) — http://localhost:4000
- Langfuse (traces) — http://localhost:3000

Assets are stored on the local filesystem (`data/assets`) by default. An S3-compatible store is
opt-in: `docker compose --profile s3 up -d`.

## Development

```bash
uv sync --all-packages --all-extras
uv run pytest -m "not integration" -q
uv run ruff check . && uv run pyright
pnpm -C apps/web dev
```

## Documentation

| doc | what it covers |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | authoritative guide: layout, rules, commands, **and what is currently broken** |
| [`docs/architecture.md`](docs/architecture.md) | what exists today |
| [`docs/belief-engine.md`](docs/belief-engine.md) | the Claim Spine being built |
| [`docs/decisions.md`](docs/decisions.md) | why the wiki is going, and what was borrowed from where |
| [`docs/operations.md`](docs/operations.md) | stack, migrations, gates |

## License

See [`LICENSE`](LICENSE). Ported third-party code carries a `NOTICE` in its package —
see [`packages/aleph-belief/NOTICE`](packages/aleph-belief/NOTICE).
