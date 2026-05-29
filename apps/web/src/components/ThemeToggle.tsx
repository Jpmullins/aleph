import { useEffect, useState } from "react";

type Mode = "light" | "dark" | "system";

const STORAGE_KEY = "aleph.theme";

function applyMode(mode: Mode) {
  const root = document.documentElement;
  if (mode === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", mode);
  }
  // CopilotKit v2 (the Live chat) keys its dark styles off a `.dark` class,
  // so mirror Aleph's effective dark state onto it.
  const prefersDark =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const isDark = mode === "dark" || (mode === "system" && !!prefersDark);
  root.classList.toggle("dark", isDark);
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
  }, []);

  const cycle = () => {
    const next: Mode = mode === "light" ? "dark" : mode === "dark" ? "system" : "light";
    setMode(next);
    applyMode(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* ignore quota errors */
    }
  };

  const icon = mode === "light" ? "☀" : mode === "dark" ? "☾" : "◐";
  const label =
    mode === "light"
      ? "Light theme (click for dark)"
      : mode === "dark"
        ? "Dark theme (click for system)"
        : "System theme (click for light)";

  return (
    <button
      type="button"
      onClick={cycle}
      title={label}
      aria-label={label}
      className={
        "rounded-md px-2 py-1 text-base hover:bg-slate-100 hover:text-slate-900 " +
        (className ?? "")
      }
      data-testid="theme-toggle"
    >
      {icon}
    </button>
  );
}
