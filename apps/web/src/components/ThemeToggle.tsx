import { useEffect, useState } from "react";

type Mode = "light" | "dark" | "system";

const STORAGE_KEY = "aleph.theme";

function applyMode(mode: Mode) {
  const root = document.documentElement;
  // "system" is resolved to an explicit light/dark attribute: the dark-mode
  // class-remap shim in tokens.css keys off [data-theme="dark"], so leaving
  // the attribute unset in system-dark would dark-flip the CSS tokens (via
  // the prefers-color-scheme block) while every hardcoded bg-white/slate-*
  // component stayed light.
  const prefersDark =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const resolved = mode === "system" ? (prefersDark ? "dark" : "light") : mode;
  root.setAttribute("data-theme", resolved);
  // CopilotKit v2 (the Live chat) keys its dark styles off a `.dark` class,
  // so mirror Aleph's effective dark state onto it.
  root.classList.toggle("dark", resolved === "dark");
}

function loadMode(): Mode {
  if (typeof window === "undefined") return "system";
  const v = window.localStorage.getItem(STORAGE_KEY);
  if (v === "light" || v === "dark" || v === "system") return v;
  return "system";
}

interface Props {
  className?: string;
}

export function ThemeToggle({ className }: Props) {
  const [mode, setMode] = useState<Mode>("system");

  useEffect(() => {
    const initial = loadMode();
    setMode(initial);
    applyMode(initial);
    // In system mode, follow OS theme changes live.
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const onChange = () => {
      if (loadMode() === "system") applyMode("system");
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
    { mode: "system", icon: "⊙", label: "System" },
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
