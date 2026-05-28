import { useEffect, useState } from "react";

import { ProjectList } from "@/components/ProjectList";
import { ProjectWorkspace } from "@/components/ProjectWorkspace";
import { handleCallback, getAccessToken, getCurrentUser, isLocalAuth, login } from "@/lib/auth";

interface Route {
  kind: "loading" | "login" | "callback" | "projects" | "workspace";
  projectId?: string;
}

function parseRoute(): Route {
  const path = window.location.pathname;
  if (path === "/auth/callback") return { kind: "callback" };
  if (path === "/" || path === "/projects") return { kind: "projects" };
  const m = path.match(/^\/projects\/([0-9a-fA-F-]+)/);
  if (m) return { kind: "workspace", projectId: m[1] };
  return { kind: "projects" };
}

function navigate(path: string) {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function App() {
  const [route, setRoute] = useState<Route>({ kind: "loading" });

  useEffect(() => {
    const onPop = () => setRoute(parseRoute());
    window.addEventListener("popstate", onPop);
    setRoute(parseRoute());
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    if (route.kind !== "callback") return;
    handleCallback()
      .then(() => navigate("/projects"))
      .catch(() => navigate("/projects"));
  }, [route.kind]);

  useEffect(() => {
    if (route.kind === "loading") return;
    if (route.kind === "callback") return;
    if (isLocalAuth()) return;
    (async () => {
      const token = await getAccessToken();
      if (!token) {
        const user = await getCurrentUser();
        if (!user) {
          setRoute({ kind: "login" });
        }
      }
    })();
  }, [route.kind]);

  if (route.kind === "loading" || route.kind === "callback") {
    return (
      <div className="flex h-full items-center justify-center text-slate-500">
        Loading…
      </div>
    );
  }
  if (route.kind === "login") {
    return (
      <div className="flex h-full items-center justify-center">
        <button
          type="button"
          onClick={() => login()}
          className="rounded-lg bg-slate-900 px-6 py-3 text-sm font-medium text-white shadow hover:bg-slate-700"
        >
          Sign in to Aleph
        </button>
      </div>
    );
  }
  if (route.kind === "workspace" && route.projectId) {
    return <ProjectWorkspace projectId={route.projectId} onBack={() => navigate("/projects")} />;
  }
  return <ProjectList onOpen={(id) => navigate(`/projects/${id}`)} />;
}
