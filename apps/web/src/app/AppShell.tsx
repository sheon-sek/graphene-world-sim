import { useEffect, type ReactNode } from "react";
import { EngineeringPage } from "../eng/EngineeringPage";
import { OperationsPage } from "../ops/OperationsPage";
import { DiagnosticsPage } from "../ops/DiagnosticsPage";
import { OpcUaPage } from "../ops/OpcUaPage";
import { BuildProgress } from "./BuildProgress";
import { Lifecycle } from "./Lifecycle";
import { href, WORKSPACES, type Workspace } from "./router";
import { usePoll, useSession } from "./session";

const LABEL: Record<Workspace, string> = {
  operations: "Operations",
  engineering: "Engineering",
  diagnostics: "Diagnostics",
  opcua: "OPC UA & Ignition",
};

function Banner() {
  const busy = useSession((s) => s.busy);
  const error = useSession((s) => s.error);
  const dismiss = useSession((s) => s.dismiss);
  if (busy)
    return (
      <div className="banner busy" role="status">
        <span className="spinner" /> {busy}
      </div>
    );
  if (error)
    return (
      <div className="banner error" role="alert">
        <pre>{error}</pre>
        <button className="icon" aria-label="Dismiss" onClick={dismiss}>
          ×
        </button>
      </div>
    );
  return null;
}

/** A session's workspaces under one bar: which session, which workspace, and its clock. */
export function AppShell({ sid, workspace }: { sid: string; workspace: Workspace }) {
  const open = useSession((s) => s.open);
  const info = useSession((s) => s.info);
  const refresh = useSession((s) => s.refresh);
  useEffect(() => {
    void open(sid);
  }, [open, sid]);
  // Running state, speed and faults can change from elsewhere (another tab, a duration ending).
  usePoll(() => refresh(), 2000, [sid]);
  let body: ReactNode = null;
  if (workspace === "operations") body = <OperationsPage sid={sid} />;
  else if (workspace === "engineering") body = <EngineeringPage sid={sid} />;
  else if (workspace === "opcua") body = <OpcUaPage sid={sid} />;
  else body = <DiagnosticsPage sid={sid} />;
  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="#/">
          Graphene World Sim
        </a>
        <span className="chip" data-testid="session-chip">
          {sid} · rev {info?.revision ?? "–"} · {info?.scope.length ?? 0} assets
        </span>
        <nav className="tabs">
          {WORKSPACES.map((w) => (
            <a key={w} href={href({ name: "session", session: sid, workspace: w })} className={w === workspace ? "on" : ""}>
              {LABEL[w]}
            </a>
          ))}
        </nav>
        <div className="spacer" />
        <Lifecycle sid={sid} />
      </header>
      <Banner />
      <BuildProgress />
      <div className="workspace">{body}</div>
    </div>
  );
}
