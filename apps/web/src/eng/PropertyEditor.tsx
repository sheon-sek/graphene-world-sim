import { useEffect, useMemo, useState } from "react";
import type { Asset, Connection, Edit } from "../api/client";
import { shortName } from "../app/format";
import { useSim } from "../live/store";
import { parametersOf, type WorldData } from "../live/world";
import { connectionId, useDraft, type Edited } from "./draft";
import { compatible, DOMAIN_COLOUR } from "./graph";

type Form = { name: string; room: string; x: string; y: string; parameters: Record<string, string> };

function formOf(asset: Asset): Form {
  return {
    name: asset.name,
    room: asset.location?.room ?? "",
    x: asset.location?.x == null ? "" : String(asset.location.x),
    y: asset.location?.y == null ? "" : String(asset.location.y),
    parameters: Object.fromEntries(Object.entries(asset.parameters ?? {}).map(([k, v]) => [k, String(v)])),
  };
}

/** Edit the selected asset's name, placement and parameters, and its connections (#54). */
export function PropertyEditor(props: {
  world: WorldData;
  edited: Edited;
  scope: Set<string>;
  shown: string[];
  stage: (edits: Edit[]) => Promise<boolean>;
}) {
  const selected = useSim((s) => s.selected);
  const asset = selected ? props.edited.assets.get(selected) : undefined;
  if (!asset) return <aside className="panel properties empty">Select an asset on the schematic to edit it.</aside>;
  return <Editor key={asset.id} asset={asset} {...props} />;
}

function Editor({
  asset,
  world,
  edited,
  scope,
  shown,
  stage,
}: {
  asset: Asset;
  world: WorldData;
  edited: Edited;
  scope: Set<string>;
  shown: string[];
  stage: (edits: Edit[]) => Promise<boolean>;
}) {
  const [form, setForm] = useState<Form>(() => formOf(asset));
  // A staged edit (here or on the schematic) becomes the form's new starting point.
  const staged = JSON.stringify(asset);
  useEffect(() => setForm(formOf(JSON.parse(staged) as Asset)), [staged]);
  const simulate = useDraft((s) => s.simulate);
  const setSimulate = useDraft((s) => s.setSimulate);
  const type = world.types.get(asset.type);
  const specs = Object.entries(type?.parameters ?? {});
  const effective = parametersOf(world, asset);
  const dirty = JSON.stringify(form) !== JSON.stringify(formOf(asset));
  const problems = specs.flatMap(([k, spec]) => {
    const raw = form.parameters[k];
    if (raw === undefined || raw === "") return [];
    const v = Number(raw);
    if (!Number.isFinite(v)) return [`${k} is not a number`];
    if (spec.min != null && v < spec.min) return [`${k} is below ${spec.min}`];
    if (spec.max != null && v > spec.max) return [`${k} is above ${spec.max}`];
    return [];
  });

  const connections = useMemo(
    () => [...edited.connections.values()].filter((c) => c.source.node === asset.id || c.target.node === asset.id),
    [edited, asset.id],
  );
  const candidates = useMemo(
    () => [
      ...shown.filter((id) => id !== asset.id).map((id) => ({ id, type: world.types.get(edited.assets.get(id)?.type ?? "") })),
      ...Object.keys(world.site.rooms ?? {}).map((r) => ({ id: `room:${r}`, type: undefined, room: true })),
    ],
    [shown, world, edited, asset.id],
  );

  function save() {
    const parameters: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(form.parameters)) {
      if (v === "") continue;
      const spec = type?.parameters?.[k];
      parameters[k] = typeof spec?.default === "string" ? v : Number(v);
    }
    const next: Asset = {
      ...asset,
      name: form.name.trim() || asset.name,
      parameters: parameters as Asset["parameters"],
      location: {
        room: form.room || null,
        x: form.x === "" ? null : Number(form.x),
        y: form.y === "" ? null : Number(form.y),
      },
    };
    void stage([{ op: "put", collection: "assets", value: next as unknown as Record<string, unknown> }]);
  }

  function remove() {
    // Removing takes the asset's connections, instruments and points with it.
    void stage([{ op: "remove", key: asset.id }]);
  }

  const inScope = scope.has(asset.id);
  return (
    <aside className="panel properties" data-testid="properties">
      <header>
        <span className="eyebrow">{type?.name ?? asset.type}</span>
        <h2>{asset.name}</h2>
        <code>{asset.id}</code>
      </header>
      {!inScope && type?.behaviour && (
        <label className="check">
          <input
            type="checkbox"
            checked={simulate.includes(asset.id)}
            onChange={(e) => setSimulate(asset.id, e.target.checked)}
            data-testid="simulate"
          />
          Simulate in this session after applying
        </label>
      )}
      <div className="form-grid">
        <label>
          Name
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label>
          Room
          <select value={form.room} onChange={(e) => setForm({ ...form, room: e.target.value })}>
            <option value="">(none)</option>
            {Object.keys(world.site.rooms ?? {})
              .sort()
              .map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
          </select>
        </label>
        <label>
          x (m)
          <input type="number" step={0.1} value={form.x} onChange={(e) => setForm({ ...form, x: e.target.value })} />
        </label>
        <label>
          y (m)
          <input type="number" step={0.1} value={form.y} onChange={(e) => setForm({ ...form, y: e.target.value })} />
        </label>
      </div>
      {specs.length > 0 && (
        <>
          <h3>Parameters</h3>
          <div className="params">
            {specs.map(([k, spec]) => (
              <label key={k} title={spec.description} className={form.parameters[k] ? "set" : ""}>
                <span>
                  {k} {spec.unit && <small>{spec.unit}</small>}
                  {spec.assumed && <small className="tag">assumed</small>}
                  <small className={`tag cc-${spec.change_class ?? "warm"}`}>{spec.change_class ?? "warm"}</small>
                </span>
                <input
                  type={typeof spec.default === "string" ? "text" : "number"}
                  placeholder={String(effective[k] ?? spec.default ?? "")}
                  min={spec.min ?? undefined}
                  max={spec.max ?? undefined}
                  value={form.parameters[k] ?? ""}
                  onChange={(e) => setForm({ ...form, parameters: { ...form.parameters, [k]: e.target.value } })}
                  data-testid={`param-${k}`}
                />
              </label>
            ))}
          </div>
        </>
      )}
      {problems.map((p) => (
        <p key={p} className="error-text">
          {p}
        </p>
      ))}
      <div className="inline-form">
        <button className="primary" disabled={!dirty || problems.length > 0} onClick={save} data-testid="stage-asset">
          Stage changes
        </button>
        <button disabled={!dirty} onClick={() => setForm(formOf(asset))}>
          Revert
        </button>
        <button className="danger" onClick={remove} title="Delete the asset and its connections">
          Delete
        </button>
      </div>
      <h3>Ports</h3>
      {Object.entries(type?.ports ?? {}).map(([port, spec]) => (
        <PortRow
          key={port}
          asset={asset}
          port={port}
          domain={spec.domain}
          direction={spec.direction ?? "both"}
          connections={connections.filter(
            (c) => (c.source.node === asset.id && c.source.port === port) || (c.target.node === asset.id && c.target.port === port),
          )}
          options={compatible(type, port, candidates, asset.id)}
          stage={stage}
        />
      ))}
    </aside>
  );
}

function PortRow({
  asset,
  port,
  domain,
  direction,
  connections,
  options,
  stage,
}: {
  asset: Asset;
  port: string;
  domain: string;
  direction: string;
  connections: Connection[];
  options: { node: string; port: string; direction: "to" | "from" }[];
  stage: (edits: Edit[]) => Promise<boolean>;
}) {
  const [choice, setChoice] = useState("");
  const other = (c: Connection) => (c.source.node === asset.id ? c.target : c.source);
  function connect() {
    const o = options.find((x) => `${x.node}|${x.port}` === choice);
    if (!o) return;
    const [source, target] =
      o.direction === "to"
        ? [{ node: asset.id, port }, { node: o.node, port: o.port }]
        : [{ node: o.node, port: o.port }, { node: asset.id, port }];
    void stage([
      {
        op: "put",
        collection: "connections",
        value: { id: connectionId(domain, source.node, target.node), domain, source, target },
      },
    ]).then((ok) => ok && setChoice(""));
  }
  return (
    <div className="port" data-testid={`port-${port}`}>
      <div className="port-head">
        <span className="swatch" style={{ background: DOMAIN_COLOUR[domain] }} />
        <b>{port}</b>
        <small>
          {domain} · {direction}
        </small>
      </div>
      {connections.map((c) => (
        <div key={c.id} className="port-conn">
          {c.source.node === asset.id ? "to" : "from"} {shortName(other(c).node)}.{other(c).port}
          <button className="icon" aria-label={`Disconnect ${c.id}`} onClick={() => void stage([{ op: "delete", collection: "connections", key: c.id }])}>
            ×
          </button>
        </div>
      ))}
      {options.length > 0 && (
        <div className="inline-form">
          <select value={choice} onChange={(e) => setChoice(e.target.value)} aria-label={`Connect ${port}`} data-testid={`connect-${port}`}>
            <option value="">Connect {direction === "in" ? "from" : "to"}…</option>
            {options.map((o) => (
              <option key={`${o.node}|${o.port}`} value={`${o.node}|${o.port}`}>
                {o.direction === "to" ? "to" : "from"} {o.node.startsWith("room:") ? `room ${o.node.slice(5)}` : shortName(o.node)}.{o.port}
              </option>
            ))}
          </select>
          <button disabled={!choice} onClick={connect} data-testid={`connect-${port}-go`}>
            Connect
          </button>
        </div>
      )}
    </div>
  );
}
