import { useEffect, useMemo } from "react";
import { useSession } from "../app/session";
import { sessionRoom, useWorld } from "../live/world";
import { DraftPanel } from "./DraftPanel";
import { overlay, useDraft } from "./draft";
import { schematic } from "./graph";
import { Library } from "./Library";
import { PropertyEditor } from "./PropertyEditor";
import { Schematic } from "./Schematic";

const NONE: never[] = [];

/** Add, place, connect and configure assets in a draft of the World Model, then apply it and
 * rebuild the session on the new revision (#53, #54). */
export function EngineeringPage({ sid }: { sid: string }) {
  const info = useSession((s) => s.info);
  const { head, draft, simulate, load, stage } = useDraft();
  useEffect(() => {
    void load();
  }, [load]);
  const { world, error } = useWorld(head);
  const operations = draft?.operations ?? NONE;
  const edited = useMemo(() => (world ? overlay(world, operations) : null), [world, operations]);
  const scope = useMemo(() => new Set(info?.scope ?? []), [info]);
  const added = useMemo(() => new Set(simulate.concat(operations.flatMap((o) => (o.op === "put" && o.collection === "assets" ? [String(o.value.id)] : [])))), [simulate, operations]);
  const shown = useMemo(
    () => (world && edited ? schematic(world, edited, scope, added).nodes.filter((n) => n.kind === "asset").map((n) => n.id) : []),
    [world, edited, scope, added],
  );
  const room = world && info ? sessionRoom(world, info.scope) : null;

  if (error) return <div className="muted pad">Could not load the World Model: {error}</div>;
  if (!world || !edited || !info) return <div className="muted pad">Loading the World Model…</div>;
  return (
    <div className="eng">
      <Library world={world} assets={edited.assets} room={room} stage={stage} />
      <Schematic world={world} edited={edited} scope={scope} added={added} stage={stage} />
      <PropertyEditor world={world} edited={edited} scope={scope} shown={shown} stage={stage} />
      <DraftPanel sid={sid} />
    </div>
  );
}
