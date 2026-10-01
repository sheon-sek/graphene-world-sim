import { Canvas, useFrame, type ThreeEvent } from "@react-three/fiber";
import { useCallback, useEffect, useMemo, useRef } from "react";
import * as THREE from "three/webgpu";
import type { PointIndex, WorldData } from "../../live/world";
import { hallOf } from "../../live/world";
import { useSim } from "../../live/store";
import { useCameraControls, useFlyToSelection, type View } from "../Camera";
import { Environment, FrameLimiter, FrameMeter } from "../HallView";
import { layoutHall, RACK } from "../hall/layout";
import { QUALITY, type Quality } from "../quality";
import { createRenderer } from "../renderer";
import {
  DOMAIN_COLOUR,
  layoutSite,
  routeConnections,
  SYSTEM_COLOUR,
  visibleFloors,
  type Route,
  type SiteAsset,
  type SiteLayout,
} from "./layout";
import { STATUS_COLOUR, statusOf } from "./status";
import "../materials";

export interface SiteOptions {
  /** Look at this floor, with the floors above it cut away; null shows them all. */
  floor: string | null;
  /** Lift each floor this many metres more above the one below, to see between them. */
  explode: boolean;
  /** Connection domains drawn. */
  layers: Set<string>;
}

const EXPLODE_M = 12;
const ROOM_COLOUR: Record<string, string> = {
  hall: "#2b3a4a",
  electrical: "#3b3524",
  cooling: "#1f3647",
  airside: "#1f3a40",
  water: "#1c3a35",
  fuel: "#3a2c20",
  support: "#2c2c36",
  core: "#30343a",
  corridor: "#262b31",
};

/** Lift per floor when exploded. */
function lift(layout: SiteLayout, floor: string, explode: boolean): number {
  if (!explode) return 0;
  return (layout.floors.find((f) => f.id === floor)?.index ?? 0) * EXPLODE_M;
}

export function useSiteLayout(world: WorldData): SiteLayout {
  return useMemo(() => {
    const links: [string, string][] = [];
    for (const c of world.connections.values()) links.push([c.source.node, c.target.node]);
    return layoutSite({
      floors: world.site.floors ?? [],
      rooms: (world.site.rooms ?? {}) as Parameters<typeof layoutSite>[0]["rooms"],
      assets: [...world.assets.values()].map((a) => ({ id: a.id, name: a.name, type: a.type, location: a.location })),
      links,
    });
  }, [world]);
}

function Rooms({ layout, options }: { layout: SiteLayout; options: SiteOptions }) {
  const shown = visibleFloors(layout, options.floor);
  const items = useMemo(
    () =>
      layout.rooms.map((r) => {
        const floor = layout.floors.find((f) => f.id === r.floor);
        return { r, elevation: floor?.elevation ?? 0, height: floor?.height ?? 4.5 };
      }),
    [layout],
  );
  const slab = useMemo(() => new THREE.BoxGeometry(1, 1, 1), []);
  const wall = useMemo(() => new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1)), []);
  return (
    <group>
      {items
        .filter(({ r }) => shown.has(r.floor))
        .map(({ r, elevation, height }) => {
          const y = elevation + lift(layout, r.floor, options.explode);
          const colour = ROOM_COLOUR[r.kind] ?? "#2a2f36";
          const wallHeight = r.outdoor ? 0.3 : Math.min(1.2, height);
          return (
            <group key={r.id} position={[r.x + r.w / 2, y, r.y + r.h / 2]} userData={{ room: r.id }}>
              <mesh geometry={slab} scale={[r.w - 0.2, 0.2, r.h - 0.2]} position={[0, -0.1, 0]} raycast={() => null}>
                <meshStandardMaterial color={colour} roughness={0.9} metalness={0} transparent opacity={r.outdoor ? 0.55 : 0.9} />
              </mesh>
              <lineSegments geometry={wall} scale={[r.w - 0.2, wallHeight, r.h - 0.2]} position={[0, wallHeight / 2, 0]} raycast={() => null}>
                <lineBasicMaterial color={r.kind === "hall" ? "#7fa7c9" : "#5d6b78"} transparent opacity={0.8} />
              </lineSegments>
            </group>
          );
        })}
    </group>
  );
}

/** Every asset as an instance of its shape, coloured by its system and its status. */
function Assets({
  layout,
  options,
  scope,
  points,
  onHover,
}: {
  layout: SiteLayout;
  options: SiteOptions;
  scope: Set<string>;
  points: PointIndex | null;
  onHover: (text: string | null) => void;
}) {
  const select = useSim((s) => s.select);
  const shown = visibleFloors(layout, options.floor);
  const groups = useMemo(() => {
    const visible = layout.assets.filter((a) => shown.has(a.floor) && a.type !== "IT Load");
    return {
      box: visible.filter((a) => a.shape === "box"),
      cylinder: visible.filter((a) => a.shape === "cylinder"),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout, options.floor]);
  const boxRef = useRef<THREE.InstancedMesh>(null);
  const cylRef = useRef<THREE.InstancedMesh>(null);
  const geometry = useMemo(
    () => ({
      box: new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0),
      cylinder: new THREE.CylinderGeometry(0.5, 0.5, 1, 16).translate(0, 0.5, 0),
    }),
    [],
  );
  const material = useMemo(() => new THREE.MeshStandardMaterial({ roughness: 0.55, metalness: 0.15 }), []);

  useEffect(() => {
    const m = new THREE.Matrix4();
    const place = (mesh: THREE.InstancedMesh | null, list: SiteAsset[]) => {
      if (!mesh) return;
      list.forEach((a, i) => {
        const [x, y, z] = a.position;
        m.makeScale(a.size[0], a.size[2], a.size[1]).setPosition(x, y + lift(layout, a.floor, options.explode), z);
        mesh.setMatrixAt(i, m);
        mesh.setColorAt(i, new THREE.Color(SYSTEM_COLOUR[a.system]));
      });
      mesh.count = list.length;
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
      mesh.computeBoundingSphere();
    };
    place(boxRef.current, groups.box);
    place(cylRef.current, groups.cylinder);
  }, [groups, layout, options.explode]);

  // Recolour from the latest frame a few times a second, outside React.
  const since = useRef(1);
  const colour = useMemo(() => new THREE.Color(), []);
  useFrame((_, dt) => {
    since.current += dt;
    if (since.current < 0.25) return;
    since.current = 0;
    const { frame } = useSim.getState();
    const faulted = new Set((frame?.faults ?? []).map((f) => f.target));
    const paint = (mesh: THREE.InstancedMesh | null, list: SiteAsset[]) => {
      if (!mesh) return;
      list.forEach((a, i) => {
        const status = statusOf(a.id, frame, points, scope, faulted);
        colour.set(status === "ok" ? SYSTEM_COLOUR[a.system] : STATUS_COLOUR[status]);
        mesh.setColorAt(i, colour);
      });
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    };
    paint(boxRef.current, groups.box);
    paint(cylRef.current, groups.cylinder);
  });

  const handlers = (list: SiteAsset[]) => ({
    onClick: (e: ThreeEvent<MouseEvent>) => {
      e.stopPropagation();
      if (e.delta > 4 || e.instanceId === undefined) return;
      select(list[e.instanceId]?.id ?? null);
    },
    onPointerMove: (e: ThreeEvent<PointerEvent>) => {
      e.stopPropagation();
      const a = e.instanceId !== undefined ? list[e.instanceId] : undefined;
      onHover(a ? `${a.name} · ${a.type}${a.room ? ` · ${a.room}` : ""}` : null);
      document.body.style.cursor = a ? "pointer" : "";
    },
    onPointerOut: () => {
      onHover(null);
      document.body.style.cursor = "";
    },
  });

  return (
    <group>
      <instancedMesh
        key={`box-${groups.box.length}`}
        ref={boxRef}
        args={[geometry.box, material, Math.max(groups.box.length, 1)]}
        {...handlers(groups.box)}
      />
      <instancedMesh
        key={`cyl-${groups.cylinder.length}`}
        ref={cylRef}
        args={[geometry.cylinder, material, Math.max(groups.cylinder.length, 1)]}
        {...handlers(groups.cylinder)}
      />
    </group>
  );
}

/** Rack rows in every hall, generated as the hall view lays them out; they stand for the
 * hall's IT load, so clicking one selects it. */
function Racks({ world, layout, options, scope }: { world: WorldData; layout: SiteLayout; options: SiteOptions; scope: Set<string> }) {
  const select = useSim((s) => s.select);
  const shown = visibleFloors(layout, options.floor);
  const racks = useMemo(() => {
    const out: { x: number; y: number; z: number; load: string | null; floor: string }[] = [];
    for (const room of layout.rooms.filter((r) => r.kind === "hall")) {
      const hall = hallOf(world, room.id, scope);
      if (!hall) continue;
      const hallLayout = layoutHall(hall);
      const load = hall.assets.find((a) => a.type === "IT Load")?.id ?? null;
      const elevation = layout.floors.find((f) => f.id === room.floor)?.elevation ?? 0;
      for (const row of hallLayout.racks)
        for (let i = 0; i < row.count; i++)
          out.push({ x: room.x + row.x0 + (i + 0.5) * RACK.w, y: elevation, z: room.y + row.z, load, floor: room.floor });
    }
    return out;
  }, [world, layout, scope]);
  const visible = useMemo(() => racks.filter((r) => shown.has(r.floor)), [racks, shown]);
  const ref = useRef<THREE.InstancedMesh>(null);
  const geometry = useMemo(() => new THREE.BoxGeometry(RACK.w * 0.92, RACK.h, RACK.d).translate(0, RACK.h / 2, 0), []);
  const material = useMemo(() => new THREE.MeshStandardMaterial({ color: "#1c2128", roughness: 0.5, metalness: 0.4 }), []);
  useEffect(() => {
    const mesh = ref.current;
    if (!mesh) return;
    const m = new THREE.Matrix4();
    visible.forEach((r, i) => {
      m.makeTranslation(r.x, r.y + lift(layout, r.floor, options.explode), r.z);
      mesh.setMatrixAt(i, m);
    });
    mesh.count = visible.length;
    mesh.instanceMatrix.needsUpdate = true;
    mesh.computeBoundingSphere();
  }, [visible, layout, options.explode]);
  return (
    <instancedMesh
      key={`racks-${visible.length}`}
      ref={ref}
      args={[geometry, material, Math.max(visible.length, 1)]}
      onClick={(e: ThreeEvent<MouseEvent>) => {
        e.stopPropagation();
        if (e.delta > 4 || e.instanceId === undefined) return;
        select(visible[e.instanceId]?.load ?? null);
      }}
    />
  );
}

function Connections({ routes, layout, options }: { routes: Route[]; layout: SiteLayout; options: SiteOptions }) {
  const shown = visibleFloors(layout, options.floor);
  const floorOfNode = useMemo(() => {
    const m = new Map<string, string>();
    for (const a of layout.assets) m.set(a.id, a.floor);
    for (const r of layout.rooms) m.set(`room:${r.id}`, r.floor);
    return m;
  }, [layout]);
  const byDomain = useMemo(() => {
    const out = new Map<string, THREE.BufferGeometry>();
    const lists = new Map<string, number[]>();
    for (const route of routes) {
      if (!options.layers.has(route.domain)) continue;
      const a = floorOfNode.get(route.from);
      const b = floorOfNode.get(route.to);
      if ((a && !shown.has(a)) || (b && !shown.has(b))) continue;
      const list = lists.get(route.domain) ?? [];
      // Exploded floors lift each end with its floor; the riser stretches between them.
      const la = a ? lift(layout, a, options.explode) : 0;
      const lb = b ? lift(layout, b, options.explode) : 0;
      const half = Math.ceil(route.points.length / 2);
      route.points.forEach((p, i) => {
        if (i === 0) return;
        const prev = route.points[i - 1];
        const l0 = i - 1 < half ? la : lb;
        const l1 = i < half ? la : lb;
        list.push(prev[0], prev[1] + l0, prev[2], p[0], p[1] + l1, p[2]);
      });
      lists.set(route.domain, list);
    }
    for (const [domain, list] of lists) {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(list, 3));
      out.set(domain, g);
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routes, options.layers, options.floor, options.explode, layout, floorOfNode]);
  useEffect(() => () => byDomain.forEach((g) => g.dispose()), [byDomain]);
  return (
    <group>
      {[...byDomain].map(([domain, g]) => (
        <lineSegments key={domain} geometry={g} raycast={() => null}>
          <lineBasicMaterial color={DOMAIN_COLOUR[domain] ?? "#ffffff"} transparent opacity={0.75} />
        </lineSegments>
      ))}
    </group>
  );
}

/** A frame around the selected asset and pulsing beacons over faulted ones. */
function Markers({ layout, options }: { layout: SiteLayout; options: SiteOptions }) {
  const selected = useSim((s) => s.selected);
  const faultKey = useSim((s) => (s.frame?.faults ?? []).map((f) => f.target).sort().join("\n"));
  const byId = useMemo(() => new Map(layout.assets.map((a) => [a.id, a])), [layout]);
  const box = useMemo(() => new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0)), []);
  const beacons = useRef<THREE.Group>(null);
  useFrame(({ clock }) => {
    const k = 0.6 + 0.4 * Math.sin(clock.elapsedTime * 5);
    beacons.current?.children.forEach((c) => c.scale.setScalar(k));
  });
  const sel = selected ? byId.get(selected) : undefined;
  const faulted = faultKey ? faultKey.split("\n").map((id) => byId.get(id)).filter((a): a is SiteAsset => !!a) : [];
  const at = (a: SiteAsset): [number, number, number] => [a.position[0], a.position[1] + lift(layout, a.floor, options.explode), a.position[2]];
  return (
    <group>
      {sel && (
        <lineSegments geometry={box} position={at(sel)} scale={[sel.size[0] + 0.6, sel.size[2] + 0.6, sel.size[1] + 0.6]} raycast={() => null}>
          <lineBasicMaterial color="#5fd4ff" />
        </lineSegments>
      )}
      <group ref={beacons}>
        {faulted.map((a) => {
          const [x, y, z] = at(a);
          return (
            <mesh key={a.id} position={[x, y + a.size[2] + 1.2, z]} raycast={() => null}>
              <sphereGeometry args={[0.6, 12, 8]} />
              <meshBasicMaterial color="#ff3b30" />
            </mesh>
          );
        })}
      </group>
    </group>
  );
}

function SiteCamera({ layout, options }: { layout: SiteLayout; options: SiteOptions }) {
  const controls = useCameraControls({ maxDistance: 400 });
  const home = useMemo<View>(() => {
    const top = options.explode ? (layout.floors.length - 1) * EXPLODE_M : 0;
    const target = new THREE.Vector3(layout.width / 2, 6 + top / 2, layout.depth / 2);
    const span = Math.max(layout.width, layout.depth);
    return { target, eye: target.clone().add(new THREE.Vector3(-span * 0.45, span * 0.55 + top, span * 0.95)) };
  }, [layout, options.explode]);
  const viewFor = useCallback(
    (id: string): View | null => {
      const a = layout.assets.find((x) => x.id === id);
      if (!a) return null;
      const y = a.position[1] + lift(layout, a.floor, options.explode);
      const target = new THREE.Vector3(a.position[0], y + a.size[2] / 2, a.position[2]);
      const reach = Math.max(8, Math.max(...a.size) * 3);
      return { target, eye: target.clone().add(new THREE.Vector3(-reach * 0.6, reach * 0.7, reach)) };
    },
    [layout, options.explode],
  );
  useFlyToSelection(controls, home, viewFor);
  return null;
}

/**
 * The whole site in 3D: every floor, room and asset of the World Model, coloured live by
 * status, with the connections of the layers chosen.
 */
export function SiteView({
  world,
  scope,
  points,
  options,
  quality = "high",
  onSlow,
  onHover,
}: {
  world: WorldData;
  scope: Set<string>;
  points: PointIndex | null;
  options: SiteOptions;
  quality?: Quality;
  onSlow?: (fps: number) => void;
  onHover: (text: string | null) => void;
}) {
  const layout = useSiteLayout(world);
  const routes = useMemo(
    () =>
      routeConnections(
        layout,
        [...world.connections.values()],
        (world.site.shafts ?? {}) as Record<string, { x: number; y: number; carries?: string[] }>,
      ),
    [layout, world],
  );
  const select = useSim((s) => s.select);
  const q = QUALITY[quality];
  return (
    <Canvas
      key={quality}
      className="hall-canvas"
      camera={{ fov: 40, near: 0.5, far: 1500, position: [-60, 80, 140] }}
      dpr={q.dpr}
      gl={async ({ canvas }) => createRenderer(canvas, { antialias: q.antialias })}
      onPointerMissed={(e) => {
        if (e.type === "click") select(null);
      }}
      frameloop={q.maxFps > 0 ? "demand" : "always"}
    >
      <Environment />
      <hemisphereLight args={["#dfe8f2", "#2a2f35", 1.0]} />
      <directionalLight position={[layout.width * 0.2, 80, layout.depth * 1.5]} intensity={1.1} color="#fff6ea" />
      <Rooms layout={layout} options={options} />
      <Racks world={world} layout={layout} options={options} scope={scope} />
      <Assets layout={layout} options={options} scope={scope} points={points} onHover={onHover} />
      <Connections routes={routes} layout={layout} options={options} />
      <Markers layout={layout} options={options} />
      <SiteCamera layout={layout} options={options} />
      {q.maxFps > 0 && <FrameLimiter fps={q.maxFps} />}
      <FrameMeter onSlow={quality === "high" ? onSlow : undefined} />
    </Canvas>
  );
}
