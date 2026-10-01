import { useEffect, useState } from "react";
import { api, must, type Preset, type SessionInfo } from "../api/client";
import { navigate } from "./router";

/** Open a running session, or create one from a preset scope. */
export function SessionsPage() {
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [creating, setCreating] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    must(api.GET("/api/runtime/sessions")).then(setSessions, (e: unknown) => setError(String(e)));
    must(api.GET("/api/runtime/presets")).then(setPresets, (e: unknown) => setError(String(e)));
  }, []);

  async function create(preset: Preset) {
    setCreating(preset.id);
    setError(null);
    try {
      const info = await must(api.POST("/api/runtime/sessions", { body: { scope: preset.scope, dt: preset.dt } }));
      await must(
        api.PUT("/api/runtime/sessions/{sid}/conditions", { params: { path: { sid: info.id } }, body: preset.conditions }),
      );
      // Serve the new session over OPC UA when nothing else is served, so Ignition sees it at once.
      const opcua = await must(api.GET("/api/opcua")).catch(() => null);
      if (opcua && opcua.session === null) await must(api.PUT("/api/opcua/session", { body: { session: info.id } }));
      navigate({ name: "session", session: info.id, workspace: "operations" });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(null);
    }
  }

  return (
    <main className="sessions-page">
      <header>
        <span className="eyebrow">Graphene World Sim</span>
        <h1>Sessions</h1>
        <p>
          A session simulates a scope of the World Model and serves its points over OPC UA. Start the whole site to
          feed Ignition every point, or a smaller scope to look around quickly. New here? Read the{" "}
          <a href="https://github.com/sheon-sek/graphene-world-sim/blob/main/docs/guide/user-guide.md">user guide</a>.
        </p>
      </header>
      {error && (
        <p className="banner error" role="alert">
          {error}
        </p>
      )}
      <section>
        <h2>Start a session</h2>
        <div className="cards">
          {presets.map((p) => (
            <article key={p.id} className="card" data-testid={`preset-${p.id}`}>
              <h3>{p.name}</h3>
              <p>{p.description}</p>
              <p className="muted">
                {p.scope.length} assets · {p.dt} s step · room {p.room}
              </p>
              <button className="primary" disabled={creating !== null} onClick={() => void create(p)}>
                {creating === p.id ? "Starting…" : "Start session"}
              </button>
              {creating === p.id && (
                <p className="muted" role="status">
                  The first start of a scope compiles its models; keep this page open. Later starts use the cache.
                </p>
              )}
            </article>
          ))}
        </div>
      </section>
      <section>
        <h2>Open sessions</h2>
        {sessions === null ? (
          <p className="muted">Loading…</p>
        ) : sessions.length === 0 ? (
          <p className="muted">No sessions yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Session</th>
                <th>Revision</th>
                <th>Assets</th>
                <th>Time</th>
                <th>State</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id}>
                  <td>{s.id}</td>
                  <td>{s.revision ?? "–"}</td>
                  <td>{s.scope.length}</td>
                  <td>{Math.round(s.t)} s</td>
                  <td>{s.running ? "Running" : "Paused"}</td>
                  <td>
                    <a href={`#/s/${encodeURIComponent(s.id)}/operations`}>Open</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <p className="muted">
        <a href="#/hero">Hero data hall</a>, a recorded FCU trip in 3D.
      </p>
    </main>
  );
}
