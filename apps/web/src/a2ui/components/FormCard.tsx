import { useState } from "react";

import { CardShell, type RendererProps } from "./_shared";

interface FieldSpec {
  name: string;
  label: string;
  type: "text" | "textarea" | "select" | "boolean";
  options?: string[];
  required?: boolean;
}

export function FormCard({ component, onAction }: RendererProps) {
  const p = component.props as {
    form_id: string;
    title: string;
    fields: FieldSpec[];
  };
  const [values, setValues] = useState<Record<string, unknown>>({});
  return (
    <CardShell title={p.title}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onAction("submit_form", { form_id: p.form_id, values });
        }}
        className="space-y-3"
      >
        {p.fields.map((f) => (
          <label key={f.name} className="block text-xs">
            <span className="mb-1 block font-medium text-ink-soft">
              {f.label}
              {f.required && <span className="text-bad"> *</span>}
            </span>
            {f.type === "textarea" && (
              <textarea
                rows={3}
                required={f.required}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [f.name]: e.target.value }))
                }
                className="w-full border border-line-strong px-2 py-1 text-sm"
              />
            )}
            {f.type === "text" && (
              <input
                type="text"
                required={f.required}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [f.name]: e.target.value }))
                }
                className="w-full border border-line-strong px-2 py-1 text-sm"
              />
            )}
            {f.type === "boolean" && (
              <input
                type="checkbox"
                onChange={(e) =>
                  setValues((v) => ({ ...v, [f.name]: e.target.checked }))
                }
              />
            )}
            {f.type === "select" && (
              <select
                required={f.required}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [f.name]: e.target.value }))
                }
                className="w-full border border-line-strong px-2 py-1 text-sm"
              >
                <option value="">— select —</option>
                {(f.options ?? []).map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
            )}
          </label>
        ))}
        <button
          type="submit"
          className="bg-ink px-3 py-1 text-xs font-medium text-ink-inverse hover:bg-ink-soft"
        >
          Submit
        </button>
      </form>
    </CardShell>
  );
}
