import { useMemo, useState } from "react";
import { shortName } from "../app/format";
import type { Scalar } from "../live/frames";
import { useSim } from "../live/store";
import type { PointIndex, WorldData } from "../live/world";

const SHOWN = 300;

function value(v: Scalar | undefined): string {
  if (v === undefined || v === null) return "–";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  const s = String(v);
  return s.length > 24 ? `${s.slice(0, 23)}…` : s;
}

/**
 * Every asset of the World Model, by floor and room, with a search. Selecting one selects it
 * in the 3D view and the inspector. Assets the World Model does not place are listed last.
 */
export function AssetTree({ world, scope }: { world: WorldData | null; scope: Set<string> }) {
  const select = useSim((s) => s.select);
  const selected = useSim((s) => s.selected);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const groups = useMemo(() => {
    if (!world) return [];
    const rooms = world.site.rooms ?? {};
    const floors = [...(world.site.floors ?? [])].sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
    const q = query.trim().toLowerCase();
    const match = (id: string, name: string, type: string) =>
      !q || id.toLowerCase().includes(q) || name.toLowerCase().includes(q) || type.toLowerCase().includes(q);
    const byRoom = new Map<string, { id: string; name: string; type: string }[]>();
    for (const a of world.assets.values()) {
      if (!match(a.id, a.name, a.type)) continue;
      const key = a.location?.room ?? "";
      byRoom.set(key, [...(byRoom.get(key) ?? []), { id: a.id, name: a.name, type: a.type }]);
    }
    const out: { key: string; title: string; assets: { id: string; name: string; type: string }[] }[] = [];
    for (const f of floors)
      for (const r of Object.values(rooms).filter((r) => r.floor === f.id).sort((a, b) => a.id.localeCompare(b.id))) {
        const list = byRoom.get(r.id);
        if (list?.length) out.push({ key: r.id, title: `${f.id} · ${r.name ?? r.id}`, assets: list.sort((a, b) => a.name.localeCompare(b.name)) });
      }
    const loose = byRoom.get("");
    if (loose?.length) out.push({ key: "", title: "Not placed", assets: loose.sort((a, b) => a.id.localeCompare(b.id)) });
    return out;
  }, [world, query]);
  const searching = query.trim() !== "";
  const toggle = (key: string) =>
    setOpen((o) => {
      const next = new Set(o);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  if (!world) return <p className="muted pad">Loading the World Model…</p>;
  const total = groups.reduce((n, g) => n + g.assets.length, 0);
  return (
    <div className="browser" data-testid="asset-tree">
      <input type="search" placeholder={`Search ${world.assets.size} assets`} value={query} onChange={(e) => setQuery(e.target.value)} />
      <p className="muted small">{total} assets in {groups.length} rooms</p>
      <ul className="tree">
        {groups.map((g) => {
          const expanded = searching || open.has(g.key);
          return (
            <li key={g.key}>
              <button className="tree-head" onClick={() => toggle(g.key)} aria-expanded={expanded}>
                {expanded ? "▾" : "▸"} {g.title} <small>{g.assets.length}</small>
              </button>
              {expanded && (
                <ul>
                  {g.assets.map((a) => (
                    <li key={a.id}>
                      <button
                        className={`tree-leaf ${selected === a.id ? "on" : ""} ${scope.has(a.id) ? "" : "idle"}`}
                        onClick={() => select(a.id)}
                        title={a.id}
                      >
                        {a.name} <small>{a.type}</small>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Every point the OPC UA server publishes, searchable by export path, with its live value
 * and quality. Selecting one selects the asset it belongs to. */
export function PointBrowser({ points }: { points: PointIndex | null }) {
  const select = useSim((s) => s.select);
  const frame = useSim((s) => s.frame);
  const [query, setQuery] = useState("");
  const [onlyBad, setOnlyBad] = useState(false);
  const paths = useMemo(() => (points ? [...points.bindings.keys()] : []), [points]);
  const q = query.trim().toLowerCase();
  const matches = useMemo(() => {
    const terms = q.split(/\s+/).filter(Boolean);
    return paths.filter((p) => {
      const lower = p.toLowerCase();
      return terms.every((t) => lower.includes(t));
    });
  }, [paths, q]);
  if (!points) return <p className="muted pad">Loading points…</p>;
  const filtered = onlyBad ? matches.filter((p) => frame?.points[p]?.quality !== "good") : matches;
  return (
    <div className="browser" data-testid="point-browser">
      <input type="search" placeholder={`Search ${paths.length.toLocaleString()} points`} value={query} onChange={(e) => setQuery(e.target.value)} />
      <label className="muted small">
        <input type="checkbox" checked={onlyBad} onChange={(e) => setOnlyBad(e.target.checked)} /> Only points not Good
      </label>
      <p className="muted small">
        {filtered.length.toLocaleString()} points{filtered.length > SHOWN ? `, first ${SHOWN} shown` : ""}
      </p>
      <ul className="point-list">
        {filtered.slice(0, SHOWN).map((path) => {
          const p = frame?.points[path];
          const owner = points.owner.get(path);
          const binding = points.bindings.get(path);
          return (
            <li key={path}>
              <button onClick={() => owner && select(owner)} title={`${path}${owner ? ` · ${shortName(owner)}` : ""}`}>
                <span className={`q q-${p?.quality ?? "none"}`} />
                <span className="path">{path}</span>
                <b>
                  {value(p?.value)}
                  {binding?.unit ? ` ${binding.unit}` : ""}
                </b>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
