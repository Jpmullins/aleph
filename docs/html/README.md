# Rendered documents

Standalone HTML copies of the two published artifacts. Open either directly in a
browser — `file://` works, no server needed.

| File | Source of record | What it is |
|---|---|---|
| `plan.html` | `../plan.md` | The plan: 57 workstreams, 340 criteria, the eight numbers that mean done |
| `backlog.html` | `../backlog.md` | Everything discussed and not built, with the UI audit and the Deep Agents adoption table |

**The markdown files are the source of record.** These are for reading and
sharing; if the two disagree, the markdown is right.

They are self-contained apart from Google Fonts (Newsreader, Public Sans,
JetBrains Mono), which load over the network and degrade to the declared
fallback stacks offline. Both follow `apps/web/src/styles/tokens.css` — the same
instrument palette as the app — and respond to the reader's light/dark
preference.

Regenerating them is manual: they were written alongside the markdown rather
than generated from it, so an edit to one does not update the other.
