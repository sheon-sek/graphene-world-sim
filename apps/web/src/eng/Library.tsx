import { useMemo, useState } from "react";
import type { Asset, ComponentType, Edit } from "../api/client";
import { useSim } from "../live/store";
import type { WorldData } from "../live/world";
import { useDraft } from "./draft";

const ORDER = ["equipment", "device", "controller", "aggregate", "support"];

function freeName(type: ComponentType, taken: Map<string, Asset>): string {
  const prefix = "L1_NEW";
  let n = 1;
  while (taken.has(`${type.id}/${prefix}${n}`)) n += 1;
  return `${prefix}${n}`;
}

/** Component types to add from, and the form that places a new asset in a room (#53). */
export function Library({
  world,
  assets,
  room,
  stage,
}: {
  world: WorldData;
  assets: Map<string, Asset>;
  room: string | null;
  stage: (edits: Edit[]) => Promise<boolean>;
}) {
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<ComponentType | null>(null);
  const [name, setName] = useState("");
  const [where, setWhere] = useState(room ?? "");
  const select = useSim((s) => s.select);
  const setSimulate = useDraft((s) => s.setSimulate);
  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const types = [...world.types.values()].filter(
      (t) => !q || t.name.toLowerCase().includes(q) || t.id.toLowerCase().includes(q) || (t.description ?? "").toLowerCase().includes(q),
    );
    return ORDER.map((c) => [c, types.filter((t) => t.category === c).sort((a, b) => a.name.localeCompare(b.name))] as const).filter(
      ([, ts]) => ts.length > 0,
    );
  }, [world, query]);
  const rooms = Object.values(world.site.rooms ?? {}).sort((a, b) => a.id.localeCompare(b.id));

  async function add() {
    if (!picked) return;
    const r = world.site.rooms?.[where];
    const id = `${picked.id}/${name.trim()}`;
    if (!name.trim() || assets.has(id)) return;
    const asset: Asset = {
      id,
      type: picked.id,
      name: name.trim(),
      parameters: {},
      location: { room: r?.id ?? null, x: r ? Math.round(r.w * 5) / 10 : null, y: r ? Math.round(r.h * 5) / 10 : null },
      system: "",
      role: "Added in the Engineering workspace",
      exported: false,
    };
    if (await stage([{ op: "put", collection: "assets", value: asset as unknown as Record<string, unknown> }])) {
      if (picked.behaviour) setSimulate(id, true);
      select(id);
      setPicked(null);
    }
  }

  return (
    <aside className="panel library" data-testid="library">
      <h3>Library</h3>
      <input type="search" placeholder="Find a type" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Find a type" />
      {picked && (
        <div className="add-form" data-testid="add-form">
          <b>Add {picked.name}</b>
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} data-testid="add-name" />
          </label>
          <label>
            Room
            <select value={where} onChange={(e) => setWhere(e.target.value)}>
              {rooms.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.id} · {r.name}
                </option>
              ))}
            </select>
          </label>
          {assets.has(`${picked.id}/${name.trim()}`) && <p className="error-text">An asset {picked.id}/{name} exists.</p>}
          <div className="inline-form">
            <button className="primary" onClick={() => void add()} data-testid="add-confirm">
              Add to draft
            </button>
            <button onClick={() => setPicked(null)}>Cancel</button>
          </div>
        </div>
      )}
      <div className="type-list">
        {groups.map(([category, types]) => (
          <section key={category}>
            <h4>{category}</h4>
            {types.map((t) => (
              <button
                key={t.id}
                className="type"
                title={t.description}
                data-testid={`type-${t.id}`}
                onClick={() => {
                  setPicked(t);
                  setName(freeName(t, assets));
                  setWhere(room ?? rooms[0]?.id ?? "");
                }}
              >
                <span>{t.name}</span>
                {t.behaviour ? <small className="tag sim">simulated</small> : <small className="tag">data only</small>}
              </button>
            ))}
          </section>
        ))}
      </div>
    </aside>
  );
}
