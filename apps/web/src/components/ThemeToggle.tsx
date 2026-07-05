import { useEffect, useState } from "react";

// Two explicit modes. The OS preference only seeds the FIRST-visit default
// (below); the toggle itself is a binary light/dark switch — no third "system"
// button.
type Mode = "light" | "dark";

const STORAGE_KEY = "aleph.theme";

function applyMode(mode: Mode) {
  const root = document.documentElement;
  root.setAttribute("data-theme", mode);
  // CopilotKit v2 (the Live chat) keys its dark styles off a `.dark` class,
  // so mirror Aleph's effective dark state onto it.
  root.classList.toggle("dark", mode === "dark");
}

function systemDefault(): Mode {
  const prefersDark =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  return prefersDark ? "dark" : "light";
}

function loadMode(): Mode {
  if (typeof window === "undefined") return "light";
  const v = window.localStorage.getItem(STORAGE_KEY);
  if (v === "light" || v === "dark") return v;
  // No explicit choice yet → seed from the OS preference.
  return systemDefault();
}

interface Props {
  className?: string;
}

export function ThemeToggle({ className }: Props) {
  const [mode, setMode] = useState<Mode>("light");

  useEffect(() => {
    const initial = loadMode();
    setMode(initial);
    applyMode(initial);
    // Until the user makes an explicit choice, follow OS theme changes live.
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const onChange = () => {
      if (window.localStorage.getItem(STORAGE_KEY) === null) {
        const d = systemDefault();
        setMode(d);
        applyMode(d);
      }
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const choose = (next: Mode) => {
    setMode(next);
    applyMode(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* ignore quota errors */
    }
  };

  const OPTIONS: { mode: Mode; icon: string; label: string }[] = [
    { mode: "light", icon: "☀", label: "Light" },
    { mode: "dark", icon: "☾", label: "Dark" },
  ];

  return (
    <div
      role="group"
      aria-label="Display theme"
      className={
        "inline-flex items-center gap-0.5 rounded-md border border-[var(--border-muted,#e2e8f0)] " +
        "bg-[var(--surface-sunken,#f8fafc)] p-0.5 " +
        (className ?? "")
      }
    >
      {OPTIONS.map(({ mode: m, icon, label }) => {
        const active = m === mode;
        return (
          <button
            key={m}
            type="button"
            onClick={() => choose(m)}
            title={`${label} theme`}
            aria-label={`${label} theme`}
            aria-pressed={active}
            className={
              "rounded px-2 py-1 text-sm leading-none transition-colors " +
              (active
                ? "font-medium"
                : "text-[var(--text-muted,#94a3b8)] hover:text-[var(--text-primary,#0f172a)]")
            }
            style={
              active
                ? { background: "var(--accent,#f97316)", color: "var(--accent-fg,#ffffff)" }
                : undefined
            }
            // Keep the legacy testid on the Dark segment so existing e2e that
            // clicks `theme-toggle` and expects data-theme ∈ {light,dark} passes.
            data-testid={m === "dark" ? "theme-toggle" : `theme-${m}`}
          >
            <span aria-hidden>{icon}</span>
          </button>
        );
      })}
    </div>
  );
}
