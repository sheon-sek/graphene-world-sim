import { useState } from "react";
import { api, apiBase, must, type Schemas } from "../api/client";
import { usePoll, useSession } from "../app/session";

type OpcUa = Schemas["OpcUaOut"];

const NAMESPACE = "urn:eetarp:graphene:demo:twin";
const CONNECTION = "Graphene Demo Twin";
const EXAMPLE = "Chiller/R_C1/Input Power";

/** The endpoint as a client on another machine or in Docker reaches it: the server binds
 * every interface (`0.0.0.0`), so the host is the one this page was loaded from. */
export function reachableEndpoint(endpoint: string, host: string): string {
  return endpoint.replace(/^(opc\.tcp:\/\/)(0\.0\.0\.0|\[::\])(?=[:/])/, `$1${host}`);
}

/**
 * What the simulator publishes over OPC UA, which session it serves, and how to connect an
 * Ignition gateway to it (ADR-0003).
 */
export function OpcUaPage({ sid }: { sid: string }) {
  const act = useSession((s) => s.act);
  const [status, setStatus] = useState<OpcUa | null>(null);
  const [missing, setMissing] = useState<string | null>(null);
  const [connection, setConnection] = useState(CONNECTION);
  const [history, setHistory] = useState("");
  usePoll(
    async () => {
      try {
        setStatus(await must(api.GET("/api/opcua")));
        setMissing(null);
      } catch (e) {
        setMissing(e instanceof Error ? e.message : String(e));
      }
    },
    3000,
    [sid],
  );

  const serve = async (session: string | null) => {
    const next = await act(null, () => must(api.PUT("/api/opcua/session", { body: { session } })));
    if (next) setStatus(next);
  };

  if (missing) return <div className="opcua muted">OPC UA is not served by this process: {missing}</div>;
  if (!status) return <div className="opcua muted">Loading…</div>;
  const endpoint = reachableEndpoint(status.endpoint, window.location.hostname || "localhost");
  const serving = status.session === sid;
  const tagsUrl = new URL(`${apiBase()}/ignition/tags`);
  tagsUrl.searchParams.set("connection", connection || CONNECTION);
  if (history) tagsUrl.searchParams.set("history", history);

  return (
    <div className="opcua">
      <section className="panel">
        <h3>OPC UA server</h3>
        <p>
          The simulator runs its own OPC UA server in the same process as this web app. It publishes one variable for
          every point of the World Model, at the same path and with the same data type as the Graphene Ignition tag
          export, so Ignition reads the simulation as if it were the real equipment.
        </p>
        <dl className="facts">
          <dt>Endpoint</dt>
          <dd>
            <code data-testid="opcua-endpoint">{endpoint}</code>
          </dd>
          <dt>Security</dt>
          <dd>Policy None, mode None, anonymous login</dd>
          <dt>Namespace</dt>
          <dd>
            <code>{NAMESPACE}</code> (index 2)
          </dd>
          <dt>Node ids</dt>
          <dd>
            <code>ns=2;s=point:&lt;export path&gt;</code>, for example <code>ns=2;s=point:{EXAMPLE}</code>; a{" "}
            <code>:</code> in a path is written <code>%3A</code>
          </dd>
          <dt>Points</dt>
          <dd>
            {status.points.toLocaleString()} published, {status.writable.toLocaleString()} of them writable commands
          </dd>
          <dt>Serving</dt>
          <dd data-testid="opcua-serving">
            {status.session === null
              ? "No session. Every point reads Bad (out of service) until one is served."
              : `Session ${status.session}, revision ${status.revision ?? "–"}${serving ? " (this session)" : ""}`}
          </dd>
        </dl>
        <div className="actions">
          {serving ? (
            <button onClick={() => void serve(null)}>Stop serving this session</button>
          ) : (
            <button className="primary" onClick={() => void serve(sid)} data-testid="opcua-serve">
              Serve this session over OPC UA
            </button>
          )}
        </div>
        <p className="muted">
          One session is served at a time. Points outside the served session's scope read Bad (out of service); start
          the Whole site preset to make every point live. Values carry the simulation time as their timestamp, so at a
          speed above 1× they run ahead of the wall clock. A write from Ignition to a command point (on/off,
          auto/manual, setpoints) is applied to the session like a command from this app; every other point is read
          only.
        </p>
      </section>

      <section className="panel">
        <h3>Connect an Ignition gateway</h3>
        <ol className="steps">
          <li>
            On the gateway's web page, open <b>Connections → OPC → Connections</b> and create an <b>OPC UA</b> connection
            named <code>{connection || CONNECTION}</code> with the endpoint <code>{endpoint}</code>, security policy{" "}
            <b>None</b> and anonymous authentication. If Ignition runs in Docker on this machine, use{" "}
            <code>host.docker.internal</code> (Docker Desktop) or the Docker bridge address such as{" "}
            <code>172.17.0.1</code> (Linux) as the host. Port 4840 must be reachable from the gateway.
          </li>
          <li>
            Create a <b>Standard</b> tag provider for the twin, for example <code>DemoTwin</code>, or use the one your
            project already has.
          </li>
          <li>
            Download the tag import file below and import it at the root of that provider in the Designer's Tag
            Browser (Import Tags, JSON). It holds the UDT definitions and every tag, each bound to its node through the
            connection named above.
          </li>
          <li>Serve a session here. The tags turn Good within a few seconds of the gateway connecting.</li>
        </ol>
        <div className="form-grid">
          <label>
            OPC UA connection name in Ignition
            <input value={connection} onChange={(e) => setConnection(e.target.value)} />
          </label>
          <label>
            Historian provider (optional)
            <input placeholder="no history" value={history} onChange={(e) => setHistory(e.target.value)} />
          </label>
        </div>
        <a className="button primary" href={tagsUrl.toString()} download="graphene-twin-tags.json" data-testid="opcua-tags">
          Download Ignition tags (JSON)
        </a>
        <p className="muted">
          A gateway that already has the Graphene <code>[DemoTwin]</code> provider needs no new tags: its item paths
          address the same node ids, so pointing its connection at this endpoint is enough.
        </p>
      </section>
    </div>
  );
}
