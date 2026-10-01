import { useFrame, type ThreeEvent } from "@react-three/fiber";
import { useMemo, useRef, type ReactNode } from "react";
import * as THREE from "three/webgpu";
import { useSim } from "../../live/store";
import { currentFrame, Eased, unitLive } from "../live";
import { materials } from "../materials";
import { DisplayTexture, louvreTexture } from "../textures";
import type { SceneAsset } from "./layout";

/** Fan speed at design air flow. The models give air flow, not shaft speed, so the view
 * turns the fans at this speed times the simulated flow fraction. */
export const DESIGN_FAN_RPM = 1450;
/** The fans turn this many times slower than their real speed, so blades stay readable. */
export const FAN_SLOW_MOTION = 12;

/** Selection and hover for everything that represents one World Model asset. */
export function Pickable({ id, children }: { id: string; children: ReactNode }) {
  const select = useSim((s) => s.select);
  const hover = useSim((s) => s.hover);
  return (
    <group
      userData={{ assetId: id }}
      onClick={(e: ThreeEvent<MouseEvent>) => {
        e.stopPropagation();
        // A drag that started on the asset orbits or pans the camera; it does not select.
        if (e.delta > 4) return;
        select(id);
      }}
      onPointerOver={(e: ThreeEvent<PointerEvent>) => {
        e.stopPropagation();
        hover(id);
        document.body.style.cursor = "pointer";
      }}
      onPointerOut={() => {
        hover(null);
        document.body.style.cursor = "";
      }}
    >
      {children}
    </group>
  );
}

function statusMaterial() {
  return new THREE.MeshBasicNodeMaterial({ color: new THREE.Color(0.2, 0.2, 0.2) });
}

const RUNNING = new THREE.Color(0.15, 3.2, 0.9);
const TRIPPED = new THREE.Color(6, 0.35, 0.2);
const IDLE = new THREE.Color(0.25, 0.27, 0.3);

/** An axial EC fan: hub, pitched blades, guard ring and spokes, and a blur disc at speed. */
function Fan({ radius, speed }: { radius: number; speed: React.RefObject<number> }) {
  const m = materials();
  const rotor = useRef<THREE.Group>(null);
  const blur = useMemo(
    () =>
      new THREE.MeshBasicNodeMaterial({
        color: "#3a4148",
        transparent: true,
        opacity: 0,
        depthWrite: false,
        side: THREE.DoubleSide,
      }),
    [],
  );
  const blades = 7;
  useFrame((_, dt) => {
    const revPerS = (speed.current ?? 0) / 60 / FAN_SLOW_MOTION;
    if (rotor.current) rotor.current.rotation.z += revPerS * Math.PI * 2 * Math.min(dt, 0.1);
    blur.opacity = Math.min(0.55, revPerS * 0.35);
  });
  return (
    <group>
      <mesh material={m.darkSteel} rotation-x={Math.PI / 2}>
        <torusGeometry args={[radius * 1.04, radius * 0.05, 8, 40]} />
      </mesh>
      {[0, 1, 2, 3].map((i) => (
        <mesh key={i} material={m.darkSteel} rotation-z={(i * Math.PI) / 4} position-z={0.03}>
          <boxGeometry args={[radius * 2.05, 0.008, 0.008]} />
        </mesh>
      ))}
      <mesh material={m.darkSteel} position-z={0.03} rotation-x={Math.PI / 2}>
        <torusGeometry args={[radius * 0.55, 0.004, 6, 32]} />
      </mesh>
      <group ref={rotor}>
        <mesh material={m.steel} rotation-x={Math.PI / 2}>
          <cylinderGeometry args={[radius * 0.22, radius * 0.25, 0.08, 24]} />
        </mesh>
        {Array.from({ length: blades }, (_, i) => (
          <group key={i} rotation-z={(i * Math.PI * 2) / blades}>
            <mesh material={m.fanBlade} position-x={radius * 0.6} rotation-x={0.45}>
              <boxGeometry args={[radius * 0.78, radius * 0.34, 0.006]} />
            </mesh>
          </group>
        ))}
      </group>
      <mesh material={blur} position-z={0.012}>
        <circleGeometry args={[radius * 0.98, 40]} />
      </mesh>
    </group>
  );
}

function StatusBeacon({ id, position }: { id: string; position: [number, number, number] }) {
  const material = useMemo(statusMaterial, []);
  const glow = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => {
    const live = unitLive(currentFrame(), id, 1);
    const pulse = 0.55 + 0.45 * Math.sin(clock.elapsedTime * 6);
    if (live.tripped) material.color.copy(TRIPPED).multiplyScalar(pulse);
    else if (live.simulated && live.energised) material.color.copy(RUNNING);
    else material.color.copy(IDLE);
    if (glow.current) glow.current.scale.setScalar(live.tripped ? 1 + pulse * 0.6 : 1);
  });
  return (
    <group position={position}>
      <mesh material={materials().darkSteel} position-y={-0.03}>
        <cylinderGeometry args={[0.045, 0.05, 0.04, 16]} />
      </mesh>
      <mesh ref={glow} material={material} position-y={0.03}>
        <cylinderGeometry args={[0.035, 0.035, 0.08, 16]} />
      </mesh>
    </group>
  );
}

/**
 * A floor-standing perimeter cooling unit, detailed as the hero asset: return-air louvres,
 * three EC fans in the discharge bay, door panels with handles, a controller display, a
 * status beacon and chilled-water connections with a control valve. Used for every
 * perimeter unit type; one that is not simulated stands still with a dark display.
 */
export function PerimeterUnit({ asset }: { asset: SceneAsset }) {
  const m = materials();
  const [W, D, H] = asset.size;
  const design = asset.parameters?.m_air_flow_nominal ?? 1;
  const speed = useRef(0);
  const eased = useRef(new Eased(0, 1.6));
  const display = useMemo(() => new DisplayTexture(), []);
  const displayMaterial = useMemo(
    () => new THREE.MeshBasicNodeMaterial({ map: display.texture }),
    [display],
  );
  const grille = useMemo(
    () => new THREE.MeshStandardNodeMaterial({ map: louvreTexture(22), metalness: 0.5, roughness: 0.4 }),
    [],
  );
  const since = useRef(1);

  useFrame((_, dt) => {
    const live = unitLive(currentFrame(), asset.id, design);
    const rpm = live.simulated && !live.tripped ? DESIGN_FAN_RPM * live.flow : 0;
    speed.current = eased.current.step(rpm, Math.min(dt, 0.1));
    since.current += dt;
    if (since.current > 0.25) {
      since.current = 0;
      if (!asset.in_scope) display.draw(asset.name, ["not simulated"], false);
      else if (live.tripped) display.draw(asset.name, ["TRIPPED", `RA ${live.returnC.toFixed(1)} C`], true);
      else
        display.draw(
          asset.name,
          [`SA ${live.supplyC.toFixed(1)} C  RA ${live.returnC.toFixed(1)}`, `fan ${Math.round(speed.current)} rpm`],
          false,
        );
    }
  });

  const body = asset.in_scope ? m.powderCoat : m.ghost;
  const front = D / 2;
  const fanR = Math.min(0.29, W / 7.5);
  return (
    <Pickable id={asset.id}>
      <group position={asset.position} rotation-y={asset.facing}>
        <mesh material={m.rubber} position-y={0.05}>
          <boxGeometry args={[W - 0.04, 0.1, D - 0.06]} />
        </mesh>
        <mesh material={body} position-y={0.1 + (H - 0.1) / 2} castShadow>
          <boxGeometry args={[W, H - 0.1, D]} />
        </mesh>
        {/* Return-air grille across the top of the front. */}
        <mesh material={grille} position={[0, H - 0.38, front + 0.004]}>
          <planeGeometry args={[W - 0.12, 0.5]} />
        </mesh>
        {/* Door panels with seams and handles. */}
        {[-1, 0, 1].map((i) => (
          <group key={i} position={[(i * W) / 3, 1.2, front + 0.006]}>
            <mesh material={body}>
              <boxGeometry args={[W / 3 - 0.02, 0.62, 0.012]} />
            </mesh>
            <mesh material={m.steel} position={[W / 6 - 0.08, 0, 0.012]}>
              <boxGeometry args={[0.02, 0.16, 0.02]} />
            </mesh>
          </group>
        ))}
        {/* Fan bay: dark recess with three EC fans. */}
        <mesh material={m.rubber} position={[0, 0.5, front + 0.002]}>
          <planeGeometry args={[W - 0.12, 0.72]} />
        </mesh>
        {[-1, 0, 1].map((i) => (
          <group key={i} position={[(i * W) / 3.2, 0.5, front + 0.02]}>
            <Fan radius={fanR} speed={speed} />
          </group>
        ))}
        {/* Controller display. */}
        <mesh material={displayMaterial} position={[W / 3, 1.36, front + 0.014]}>
          <planeGeometry args={[0.34, 0.17]} />
        </mesh>
        {/* Nameplate. */}
        <mesh material={m.steel} position={[-W / 3, 1.36, front + 0.014]}>
          <planeGeometry args={[0.22, 0.07]} />
        </mesh>
        {/* Chilled-water connections and control valve on the roof. */}
        {[-0.55, -0.25].map((x, i) => (
          <group key={x} position={[x * (W / 2.1), H, -D * 0.15]}>
            <mesh material={i === 0 ? m.steel : m.copper} position-y={0.12}>
              <cylinderGeometry args={[0.055, 0.055, 0.24, 20]} />
            </mesh>
            <mesh material={m.darkSteel} position-y={0.07}>
              <cylinderGeometry args={[0.075, 0.075, 0.05, 20]} />
            </mesh>
          </group>
        ))}
        <mesh material={m.darkSteel} position={[(-0.4 * W) / 2.1, H + 0.3, -D * 0.15]}>
          <boxGeometry args={[0.14, 0.12, 0.12]} />
        </mesh>
        {asset.in_scope && <StatusBeacon id={asset.id} position={[W / 2 - 0.12, H + 0.04, front - 0.12]} />}
      </group>
    </Pickable>
  );
}

/**
 * Ceiling cooling units hung over a hot aisle: slim coil cabinets on drop rods, drawing hot
 * air up out of the aisle and discharging cool air sideways through fans into the cold
 * aisles either side.
 */
export function CeilingCoils({ asset }: { asset: SceneAsset }) {
  const m = materials();
  const [len] = asset.size;
  const count = Math.max(1, Math.floor(len / 1.6));
  const pitch = len / count;
  const design = asset.parameters?.m_air_flow_nominal ?? 1;
  const speed = useRef(0);
  const eased = useRef(new Eased(0, 1.6));
  useFrame((_, dt) => {
    const live = unitLive(currentFrame(), asset.id, design);
    speed.current = eased.current.step(
      live.simulated && !live.tripped ? DESIGN_FAN_RPM * 1.4 * live.flow : 0,
      Math.min(dt, 0.1),
    );
  });
  const body = asset.in_scope ? m.powderCoat : m.ghost;
  return (
    <Pickable id={asset.id}>
      <group position={asset.position}>
        {Array.from({ length: count }, (_, i) => {
          const x = -len / 2 + pitch * (i + 0.5);
          return (
            <group key={i} position-x={x}>
              <mesh material={body} castShadow>
                <boxGeometry args={[pitch - 0.25, 0.32, 1.0]} />
              </mesh>
              {[-1, 1].map((side) => (
                <group key={side} position={[0, 0, side * 0.505]} rotation-y={side === 1 ? 0 : Math.PI}>
                  {[-0.3, 0.3].map((fx) => (
                    <group key={fx} position-x={fx * (pitch / 1.6)}>
                      <Fan radius={0.12} speed={speed} />
                    </group>
                  ))}
                </group>
              ))}
              {[-1, 1].map((side) => (
                <mesh key={side} material={m.steel} position={[side * (pitch / 2 - 0.2), 0.5, 0]}>
                  <cylinderGeometry args={[0.01, 0.01, 0.7, 6]} />
                </mesh>
              ))}
            </group>
          );
        })}
        {asset.in_scope && <StatusBeacon id={asset.id} position={[len / 2 - 0.2, 0.2, 0]} />}
      </group>
    </Pickable>
  );
}

/** The chilled-water branch into the hall: supply and return headers, valves and a meter. */
export function CoolingBlock({ asset }: { asset: SceneAsset }) {
  const m = materials();
  const [W] = asset.size;
  return (
    <Pickable id={asset.id}>
      <group position={asset.position} rotation-y={asset.facing}>
        <mesh material={m.powderDark} position={[0, 0.05, -0.25]}>
          <boxGeometry args={[W, 0.1, 0.4]} />
        </mesh>
        {[
          [0.8, m.steel],
          [1.3, m.copper],
        ].map(([y, mat]) => (
          <group key={y as number} position-y={y as number}>
            <mesh material={mat as THREE.Material} rotation-z={Math.PI / 2}>
              <cylinderGeometry args={[0.08, 0.08, W, 24]} />
            </mesh>
            {[-0.5, 0, 0.5].map((x) => (
              <group key={x} position-x={x * W * 0.8}>
                <mesh material={m.darkSteel}>
                  <boxGeometry args={[0.14, 0.2, 0.2]} />
                </mesh>
                <mesh material={m.trayYellow} position-z={0.14}>
                  <cylinderGeometry args={[0.012, 0.012, 0.16, 8]} />
                </mesh>
              </group>
            ))}
          </group>
        ))}
        <mesh material={m.powderCoat} position={[W / 2 - 0.2, 1.05, 0.05]}>
          <boxGeometry args={[0.26, 0.26, 0.12]} />
        </mesh>
      </group>
    </Pickable>
  );
}

export function WallPanel({ asset }: { asset: SceneAsset }) {
  const m = materials();
  const [W, D, H] = asset.size;
  return (
    <Pickable id={asset.id}>
      <group position={asset.position} rotation-y={asset.facing}>
        <mesh material={m.powderDark}>
          <boxGeometry args={[W, H, D]} />
        </mesh>
        <mesh material={m.steel} position={[W / 2 - 0.08, 0, D / 2 + 0.01]}>
          <boxGeometry args={[0.02, 0.14, 0.02]} />
        </mesh>
      </group>
    </Pickable>
  );
}

/** A sensor on a drop rod from the service zone. */
export function Sensor({ asset, ceiling }: { asset: SceneAsset; ceiling: number }) {
  const m = materials();
  const [x, y, z] = asset.position;
  return (
    <Pickable id={asset.id}>
      <group position={[x, y, z]}>
        <mesh material={m.powderCoat}>
          <boxGeometry args={[0.1, 0.12, 0.05]} />
        </mesh>
        <mesh material={m.steel} position-y={(ceiling - y) / 2}>
          <cylinderGeometry args={[0.006, 0.006, ceiling - y, 6]} />
        </mesh>
      </group>
    </Pickable>
  );
}

export function CeilingDevice({ asset }: { asset: SceneAsset }) {
  const m = materials();
  return (
    <Pickable id={asset.id}>
      <mesh material={m.powderCoat} position={asset.position}>
        <cylinderGeometry args={[0.07, 0.08, 0.05, 20]} />
      </mesh>
    </Pickable>
  );
}

export function FloorCable({ asset }: { asset: SceneAsset }) {
  const m = materials();
  return (
    <Pickable id={asset.id}>
      <mesh material={m.powderDark} position={[asset.position[0], 0.03, asset.position[2]]}>
        <boxGeometry args={[0.24, 0.06, 0.16]} />
      </mesh>
    </Pickable>
  );
}
