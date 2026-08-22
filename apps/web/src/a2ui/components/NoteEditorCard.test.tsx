/**
 * The autosave, and the badge that reports on it.
 *
 * Both were lies, in the same six lines. The write was guarded on `section_id`
 * — which `_notes_messages` binds as `null` for any note that has no sections
 * — so typing into such a note dispatched nothing at all. And `setSaved("saved")`
 * sat on the line *after* the dispatch, so the badge said "Saved" before the
 * request had left the browser: an autosave that 500s reported success.
 *
 * Two failure modes, one screen, and both look exactly like a working editor.
 */
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NoteEditorCard } from "@/a2ui/components/NoteEditorCard";
import type { A2UIComponent } from "@/a2ui/catalog";

const NOTE_ID = "33333333-3333-4333-8333-333333333333";
const SECTION_ID = "44444444-4444-4444-8444-444444444444";

function card(props: Record<string, unknown>): A2UIComponent {
  return { type: "NoteEditorCard", id: "n", props } as A2UIComponent;
}

function mount(props: Record<string, unknown>, onAction: (a: string, p: Record<string, unknown>) => void | Promise<boolean>) {
  return render(<NoteEditorCard component={card(props)} onAction={onAction} />);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

/** Push past the 700ms autosave debounce and let the promise settle. */
async function settle() {
  await act(async () => {
    vi.advanceTimersByTime(800);
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("NoteEditorCard autosave", () => {
  it("writes through the section when the note has one", async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const view = mount({ note_id: NOTE_ID, section_id: SECTION_ID, body_md: "" }, (a, p) => {
      calls.push([a, p]);
      return Promise.resolve(true);
    });
    fireEvent.change(view.getByTestId("note-editor"), { target: { value: "typed" } });
    await settle();
    expect(calls).toEqual([["edit_note", { section_id: SECTION_ID, body_md: "typed" }]]);
  });

  it("writes a SECTIONLESS note through its note id instead of discarding the edit", async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const view = mount({ note_id: NOTE_ID, section_id: null, body_md: "" }, (a, p) => {
      calls.push([a, p]);
      return Promise.resolve(true);
    });
    fireEvent.change(view.getByTestId("note-editor"), { target: { value: "typed" } });
    await settle();
    expect(calls).toEqual([["edit_note", { note_id: NOTE_ID, body_md: "typed" }]]);
  });

  it("says Saved only once the router has answered, never before", async () => {
    let resolve: ((ok: boolean) => void) | null = null;
    const view = mount(
      { note_id: NOTE_ID, section_id: SECTION_ID, body_md: "" },
      () => new Promise<boolean>((r) => (resolve = r)),
    );
    fireEvent.change(view.getByTestId("note-editor"), { target: { value: "typed" } });
    await settle();
    // Dispatched, in flight, unanswered — and the badge must not claim success.
    expect(view.getByTestId("note-save-state").textContent).toBe("Saving…");
    await act(async () => {
      resolve?.(true);
      await Promise.resolve();
    });
    await waitFor(() => expect(view.getByTestId("note-save-state").textContent).toBe("Saved"));
  });

  it("says Not saved when the write failed, rather than Saved", async () => {
    const view = mount({ note_id: NOTE_ID, section_id: SECTION_ID, body_md: "" }, () =>
      Promise.resolve(false),
    );
    fireEvent.change(view.getByTestId("note-editor"), { target: { value: "typed" } });
    await settle();
    await waitFor(() => expect(view.getByTestId("note-save-state").textContent).toBe("Not saved"));
  });

  it("says Not saved when there is neither a section nor a note to write to", async () => {
    const calls: string[] = [];
    const view = mount({ section_id: null, body_md: "" }, (a) => {
      calls.push(a);
      return Promise.resolve(true);
    });
    fireEvent.change(view.getByTestId("note-editor"), { target: { value: "typed" } });
    await settle();
    expect(calls).toEqual([]);
    expect(view.getByTestId("note-save-state").textContent).toBe("Not saved");
  });
});
