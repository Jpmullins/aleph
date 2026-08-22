# The browser specs moved to `tests/playwright/specs/`

WS-P4 harvested the seven `.spec.ts` files that lived here. They are not copied,
they are **moved**: three hand-maintained copies of a catalog already taught this
repository what two locations for one artifact costs, and a browser suite is no
different.

They could not have run from here in any case. `audit/run.sh` resolves its
Playwright binary through `audit/checks/e2e/node_modules`, a symlink into
`tests/playwright/node_modules` — and `tests/playwright/` had been deleted in the
harness reset, so `E2E_OK` was never set and all seven checks reported SKIP
forever while `run.sh` reported no failures. That is the exact shape CLAUDE.md
warns about under "A green `audit/run.sh` is weaker evidence than it looks".

Two of the seven no longer described the app and were rewritten during the move,
which is the other reason they should live in one place:

- `workspace-three-panel-shell` required five surface tabs above the right
  panel. The tab bar is gone; surfaces come from
  `GET /v1/projects/{id}/panes` and the reading region is a canvas of blocks.
  The spec now asserts the rail against the server's own registry.
- `wikilink-navigation` intercepted `GET /wiki/pages`. `WikiSurface` has not
  fetched since WP-4 — it renders only from the surface data model — so the
  mock changed nothing and the spec timed out on markup that could never
  appear. It now fulfils the multiplexed `/surfaces/stream`.

Run them:

    docker compose -f deploy/compose/docker-compose.yml up -d --wait
    pnpm -C tests/playwright install
    pnpm -C tests/playwright exec playwright install chromium
    ALEPH_WEB_BASE_URL=http://localhost:5273 \
    ALEPH_API_BASE_URL=http://localhost:8000 \
      pnpm -C tests/playwright test

`audit/run.sh`'s seven `e2e` claims now report `error|no e2e spec …` rather than
`skip`, which is the honest reading: the subject is not missing from this
machine, it is not here. `docs/plan.md` (the cleanup table) has `audit/` slated
for removal as a second acceptance gate that disagrees with the first, with the
harvest of these specs named as its precondition. This is that harvest.
