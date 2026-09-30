import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three/webgpu";
import { float, fract, mix, smoothstep, uniform, uv, vec3 } from "three/tsl";
import { currentFrame, Eased, stateNumber, unitLive } from "../live";
import { materials } from "../materials";
import { paintField, rackRise, temperatureColour, type FieldInputs, type FieldUnit } from "../thermal";
import type { HallLayout, SceneAsset } from "./layout";

const SUPPLY_Y = 3.85;
const RETURN_Y = 4.1;
/** Pipes are sized for this water velocity at the unit's design flow. */
const DESIGN_VELOCITY = 1.5;

type V3 = [number, number, number];

function rotate([x, y, z]: V3, facing: number): V3 {
  const c = Math.cos(facing);
  const s = Math.sin(facing);
  return [x * c + z * s, y, -x * s + z * c];
}

function at(asset: SceneAsset, local: V3): V3 {
  const [x, y, z] = rotate(local, asset.facing);
  return [asset.position[0] + x, asset.position[1] + y, asset.position[2] + z];
}

/** Where a unit's chilled-water supply and return connect, in scene coordinates. */
export function connections(asset: SceneAsset): { supply: V3; ret: V3 } | null {
  const [W, D, H] = asset.size;
  if (asset.kind === "fan-coil" || asset.kind === "perimeter") {
    return {
      supply: at(asset, [-0.55 * (W / 2.1), H + 0.24, -D * 0.15]),
      ret: at(asset, [-0.25 * (W / 2.1), H + 0.24, -D * 0.15]),
    };
  }
  if (asset.kind === "ceiling-coils") {
    return { supply: at(asset, [-W / 2 + 0.4, 0.16, -0.2]), ret: at(asset, [-W / 2 + 0.4, 0.16, 0.2]) };
  }
  if (asset.kind === "cooling-block") {
    return { supply: at(asset, [-W / 2 + 0.15, 0.8, 0]), ret: at(asset, [-W / 2 + 0.45, 1.3, 0]) };
  }
  return null;
}

/** A Manhattan route between two points at `level`, as a curve with rounded corners. */
export function route(from: V3, to: V3, level: number, lane: number): THREE.CurvePath<THREE.Vector3> {
  const pts: V3[] = [
    from,
    [from[0], level, from[2]],
    [from[0] + lane, level, from[2]],
    [from[0] + lane, level, to[2]],
    [to[0], level, to[2]],
    to,
  ].filter((p, i, all) => i === 0 || p.some((v, k) => Math.abs(v - all[i - 1][k]) > 1e-3)) as V3[];
  const path = new THREE.CurvePath<THREE.Vector3>();
  const v = pts.map((p) => new THREE.Vector3(...p));
  const r = 0.18;
  let start = v[0];
  for (let i = 1; i < v.length - 1; i++) {
    const a = v[i].clone().sub(v[i - 1]);
    const b = v[i + 1].clone().sub(v[i]);
    const ra = Math.min(r, a.length() / 2);
    const rb = Math.min(r, b.length() / 2);
    const p0 = v[i].clone().sub(a.normalize().multiplyScalar(ra));
    const p1 = v[i].clone().add(b.normalize().multiplyScalar(rb));
    if (p0.distanceTo(start) > 1e-4) path.add(new THREE.LineCurve3(start, p0));
    path.add(new THREE.QuadraticBezierCurve3(p0, v[i].clone(), p1));
    start = p1;
  }
  path.add(new THREE.LineCurve3(start, v[v.length - 1]));
  return path;
}

function flowMaterial() {
  const tint = uniform(new THREE.Color(0.2, 0.5, 1));
  const offset = uniform(0);
  const length = uniform(1);
  const active = uniform(1);
  const material = new THREE.MeshStandardNodeMaterial({ metalness: 0.8, roughness: 0.3 });
  const phase = fract(uv().x.mul(length).sub(offset).div(0.9));
  const pulse = smoothstep(0.0, 0.06, phase).mul(float(1).sub(smoothstep(0.06, 0.4, phase)));
  material.colorNode = mix(vec3(0.72, 0.75, 0.78), tint, 0.6);
  material.emissiveNode = tint.mul(pulse.mul(active).mul(1.1).add(0.03));
  return { material, tint, offset, length, active };
}

interface PipeSpec {
  key: string;
  curve: THREE.CurvePath<THREE.Vector3>;
  unit: string;
  supply: boolean;
  design: number;
}

export function pipeRuns(layout: HallLayout): PipeSpec[] {
  const block = layout.assets.find((a) => a.kind === "cooling-block" && a.in_scope);
  if (!block) return [];
  const origin = connections(block);
  if (!origin) return [];
  const units = layout.assets.filter(
    (a) => a.in_scope && (a.kind === "fan-coil" || a.kind === "ceiling-coils"),
  );
  const runs: PipeSpec[] = [];
  units.forEach((u, i) => {
    const c = connections(u);
    if (!c) return;
    const lane = 0.45 + i * 0.5;
    const design = u.parameters?.m_wat_flow_nominal ?? 5;
    runs.push({ key: `${u.id}:s`, curve: route(origin.supply, c.supply, SUPPLY_Y, lane), unit: u.id, supply: true, design });
    runs.push({ key: `${u.id}:r`, curve: route(origin.ret, c.ret, RETURN_Y, lane + 0.3), unit: u.id, supply: false, design });
  });
  return runs;
}

function Pipe({ spec }: { spec: PipeSpec }) {
  const flow = useMemo(flowMaterial, []);
  const length = useMemo(() => spec.curve.getLength(), [spec]);
  const geometry = useMemo(
    () => new THREE.TubeGeometry(spec.curve, Math.max(24, Math.round(length * 6)), 0.065, 16, false),
    [spec, length],
  );
  useEffect(() => () => geometry.dispose(), [geometry]);
  const velocity = useRef(new Eased(0, 1.2));
  const colour = useMemo(() => new THREE.Color(), []);
  flow.length.value = length;
  useFrame((_, dt) => {
    const live = unitLive(currentFrame(), spec.unit, 1);
    const v = spec.design > 0 ? (DESIGN_VELOCITY * live.chwFlow) / spec.design : 0;
    const speed = velocity.current.step(v, Math.min(dt, 0.1));
    flow.offset.value += speed * Math.min(dt, 0.1);
    flow.active.value = Math.min(1, speed / 0.3);
    const [r, g, b] = temperatureColour(spec.supply ? live.chwSupplyC : live.chwReturnC);
    flow.tint.value = colour.setRGB(r, g, b);
  });
  return (
    <mesh geometry={geometry} material={flow.material} userData={{ pipe: spec.key }} />
  );
}

export function Pipes({ layout }: { layout: HallLayout }) {
  const runs = useMemo(() => pipeRuns(layout), [layout]);
  const m = materials();
  return (
    <group>
      {runs.map((spec) => (
        <Pipe key={spec.key} spec={spec} />
      ))}
      {runs
        .filter((r) => r.supply)
        .map((r) => {
          const p = r.curve.getPoint(0.5);
          return (
            <mesh key={r.key} material={m.galvanised} position={[p.x, 4.35, p.z]}>
              <boxGeometry args={[0.06, 0.5, 0.06]} />
            </mesh>
          );
        })}
    </group>
  );
}

/** Field inputs from the latest frame, eased so the field drifts rather than jumps. */
export function useFieldInputs(layout: HallLayout, roomId: string) {
  const ref = useRef<FieldInputs | null>(null);
  const eased = useRef(new Map<string, Eased>());
  const ease = (key: string, target: number, dt: number, tau = 2.5) => {
    let e = eased.current.get(key);
    if (!e) {
      e = new Eased(target, tau);
      eased.current.set(key, e);
    }
    return e.step(target, dt);
  };
  useFrame((_, dtRaw) => {
    const dt = Math.min(dtRaw, 0.1);
    const frame = currentFrame();
    if (!frame) return;
    const room = ease("room", stateNumber(frame, `room:${roomId}`, "TAir", 298.15) - 273.15, dt);
    const units: FieldUnit[] = [];
    let air = 0;
    for (const a of layout.assets) {
      if (!a.in_scope || (a.kind !== "fan-coil" && a.kind !== "ceiling-coils")) continue;
      const design = a.parameters?.m_air_flow_nominal ?? 1;
      const live = unitLive(frame, a.id, design);
      const flow = ease(`${a.id}:flow`, live.tripped ? 0 : live.flow, dt, 1.5);
      const supply = ease(`${a.id}:sa`, live.supplyC, dt);
      air += stateNumber(frame, a.id, "mAir_flow");
      if (a.kind === "ceiling-coils") {
        const [len] = a.size;
        for (let k = 0; k < 6; k++) {
          const x = a.position[0] - len / 2 + (len * (k + 0.5)) / 6;
          for (const side of [-1, 1]) units.push({ x, z: a.position[2] + side * 2.0, supply, flow, reach: 2.4 });
        }
      } else {
        const [x, , z] = a.position;
        const reach = 7;
        const out = [Math.sin(a.facing) * 2.5, Math.cos(a.facing) * 2.5];
        units.push({ x: x + out[0], z: z + out[1], supply, flow, reach });
      }
    }
    const it = layout.assets.find((a) => a.kind === "it-load" && a.in_scope);
    const itW = it ? stateNumber(frame, it.id, "P") : 0;
    ref.current = {
      room,
      rackRise: ease("rise", rackRise(itW, air), dt),
      units,
      hotAisles: layout.hotAisles,
    };
  });
  return ref;
}

const NX = 96;
const NZ = 120;

/** The air-temperature field as a translucent slice through the hall at inlet height. */
export function AirField({
  layout,
  field,
  visible,
}: {
  layout: HallLayout;
  field: React.RefObject<FieldInputs | null>;
  visible: boolean;
}) {
  const { texture, data } = useMemo(() => {
    const data = new Uint8Array(NX * NZ * 4);
    const texture = new THREE.DataTexture(data, NX, NZ, THREE.RGBAFormat);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.magFilter = THREE.LinearFilter;
    texture.minFilter = THREE.LinearFilter;
    texture.flipY = false;
    return { texture, data };
  }, []);
  const material = useMemo(
    () =>
      new THREE.MeshBasicNodeMaterial({
        map: texture,
        transparent: true,
        opacity: 0.32,
        depthWrite: false,
        side: THREE.DoubleSide,
      }),
    [texture],
  );
  const since = useRef(1);
  useFrame(({ camera }, dt) => {
    since.current += dt;
    // Fade the slice out as the camera comes down to inspect a unit.
    const h = camera.position.y;
    material.opacity = 0.34 * Math.min(1, Math.max(0, (h - 3.5) / 6));
    const inputs = field.current;
    if (!inputs || !visible || since.current < 0.15) return;
    since.current = 0;
    paintField(inputs, layout.width, layout.depth, NX, NZ, data);
    texture.needsUpdate = true;
  });
  return (
    <mesh
      visible={visible}
      material={material}
      rotation-x={Math.PI / 2}
      position={[layout.width / 2, 1.25, layout.depth / 2]}
      raycast={() => null}
    >
      <planeGeometry args={[layout.width, layout.depth]} />
    </mesh>
  );
}
