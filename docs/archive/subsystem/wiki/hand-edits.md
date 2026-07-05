# Hand-edits

Analyst edits to wiki pages are sticky. The wiki agent must not clobber
them on the next compile.

## Mechanism

`HandEditMark(page_id, section_anchor, body_sha256_at_edit, applied_at, applied_by)`.
One row per active mark. `cleared_at` is set when the analyst clears
the protection.

On every `wiki_service.commit_revision`:

1. Read all active marks for the page.
2. Build a `set[section_anchor]` of protected anchors.
3. Split the agent-proposed body into sections by heading anchor.
4. For each protected anchor, splice the prior revision's text in place
   of the agent's proposed text.

The protected sections appear byte-for-byte unchanged across revisions
until the analyst clears the mark.

## UX (Inc 1 API only; full UI Inc 4)

- `POST /v1/projects/{id}/wiki/pages/{page_id}/sections/{anchor}/handedit`
  — owner/editor only. Records the current body sha256 and the
  applying user.
- `DELETE …/handedit` — clears.

The wiki tab in Inc 1 shows a `✎` badge on protected sections (Inc 4's
A2UI surface adds the full edit-with-protect affordance).

## What about whole-page hand-edits?

`HandEditMark.section_anchor` is nullable. A null anchor means "the
whole page is protected" — the next compile skips this page entirely.
This is a heavy hammer; the section-level mark is the recommended path.
