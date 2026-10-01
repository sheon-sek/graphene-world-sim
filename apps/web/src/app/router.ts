import { useSyncExternalStore } from "react";

/**
 * Hash routes, so the built app can be served from any path:
 * `#/` sessions, `#/s/<id>/<workspace>` a session, `#/hero` the recorded hero hall.
 */
export type Route =
  | { name: "sessions" }
  | { name: "hero" }
  | { name: "session"; session: string; workspace: Workspace };

export type Workspace = "operations" | "engineering" | "diagnostics" | "opcua";
export const WORKSPACES: Workspace[] = ["operations", "engineering", "diagnostics", "opcua"];

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "hero") return { name: "hero" };
  if (parts[0] === "s" && parts[1]) {
    const workspace = WORKSPACES.includes(parts[2] as Workspace) ? (parts[2] as Workspace) : "operations";
    return { name: "session", session: parts[1], workspace };
  }
  return { name: "sessions" };
}

export function href(route: Route): string {
  if (route.name === "hero") return "#/hero";
  if (route.name === "session") return `#/s/${encodeURIComponent(route.session)}/${route.workspace}`;
  return "#/";
}

export function navigate(route: Route): void {
  window.location.hash = href(route);
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener("hashchange", onChange);
  return () => window.removeEventListener("hashchange", onChange);
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash);
  return parseRoute(hash);
}
