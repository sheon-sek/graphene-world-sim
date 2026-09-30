import { useFrame } from "@react-three/fiber";
import { useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three/webgpu";
import { float, fract, hash, instanceIndex, mix, step, time, vec3 } from "three/tsl";
import { materials } from "../materials";
import { perforatedTexture, floorTextures } from "../textures";
import { fieldAt, temperatureColour, type FieldInputs } from "../thermal";
import { HOT_AISLE_WIDTH, RACK, type HallLayout } from "./layout";

const LEDS_PER_RACK = 10;

/** Floor, raised-floor edge and walls. The two walls nearest the default camera are cut low. */
export function Shell({ layout }: { layout: HallLayout }) {
  const m = materials();
  const floor = useMemo(() => {
    const { map, rough } = floorTextures(layout);
    return new THREE.MeshStandardNodeMaterial({ map, roughnessMap: rough, roughness: 0.85, metalness: 0.15 });
  }, [layout]);
  const { width: W, depth: D, height: H } = layout;
  return (
    <group>
      <mesh rotation-x={-Math.PI / 2} position={[W / 2, 0, D / 2]} material={floor} receiveShadow>
        <planeGeometry args={[W, D]} />
      </mesh>
      {/* The raised floor's edge. Its top stops just under the floor: at the floor's height
          the two surfaces z-fight and the floor breaks up as the camera moves. */}
      <mesh position={[W / 2, -0.31, D / 2]} material={m.wallDark}>
        <boxGeometry args={[W + 0.02, 0.6, D + 0.02]} />
      </mesh>
      <mesh rotation-x={-Math.PI / 2} position={[W / 2, -0.61, D / 2]} material={m.wallDark}>
        <planeGeometry args={[W + 40, D + 40]} />
      </mesh>
      {/* Full-height walls on the far sides, with a dark skirting band. */}
      <mesh position={[W / 2, H / 2, -0.1]} material={m.wall}>
        <boxGeometry args={[W + 0.4, H, 0.2]} />
      </mesh>
      <mesh position={[W / 2, 0.15, 0.005]} material={m.wallDark}>
        <boxGeometry args={[W, 0.3, 0.02]} />
      </mesh>
      <mesh position={[W + 0.1, H / 2, D / 2]} material={m.wall}>
        <boxGeometry args={[0.2, H, D + 0.4]} />
      </mesh>
      <mesh position={[W - 0.005, 0.15, D / 2]} material={m.wallDark}>
        <boxGeometry args={[0.02, 0.3, D]} />
      </mesh>
      {/* Cut-away walls on the near sides. */}
      <mesh position={[W / 2, 0.2, D + 0.1]} material={m.wallDark}>
        <boxGeometry args={[W + 0.4, 0.4, 0.2]} />
      </mesh>
      <mesh position={[-0.1, 0.2, D / 2]} material={m.wallDark}>
        <boxGeometry args={[0.2, 0.4, D + 0.4]} />
      </mesh>
    </group>
  );
}

function rackMatrices(layout: HallLayout) {
  const body: THREE.Matrix4[] = [];
  const doors: THREE.Matrix4[] = [];
  const leds: THREE.Matrix4[] = [];
  const bars: THREE.Matrix4[] = [];
  const fronts: [number, number][] = [];
  const q = new THREE.Quaternion();
  const up = new THREE.Vector3(0, 1, 0);
  const one = new THREE.Vector3(1, 1, 1);
  for (const row of layout.racks) {
    const turn = row.front === 1 ? 0 : Math.PI;
    for (let i = 0; i < row.count; i++) {
      const x = row.x0 + (i + 0.5) * RACK.w;
      body.push(new THREE.Matrix4().makeTranslation(x, RACK.h / 2, row.z));
      for (const side of [1, -1]) {
        const face = row.z + row.front * side * (RACK.d / 2 + 0.004);
        q.setFromAxisAngle(up, side === 1 ? turn : turn + Math.PI);
        doors.push(new THREE.Matrix4().compose(new THREE.Vector3(x, RACK.h / 2, face), q.clone(), one));
      }
      const front = row.z + row.front * (RACK.d / 2 + 0.008);
      fronts.push([x, row.z + row.front * (RACK.d / 2 + 0.6)]);
      q.setFromAxisAngle(up, turn);
      for (let k = 0; k < LEDS_PER_RACK; k++) {
        const y = 0.35 + (k * 1.6) / LEDS_PER_RACK;
        const off = ((k * 37) % 5) * 0.03 - 0.06;
        leds.push(new THREE.Matrix4().compose(new THREE.Vector3(x + off - 0.1, y, front), q.clone(), one));
      }
      bars.push(new THREE.Matrix4().compose(new THREE.Vector3(x, RACK.h - 0.06, front + row.front * 0.002), q.clone(), one));
    }
  }
  return { body, doors, leds, bars, fronts };
}

/**
 * The racks, instanced: bodies, perforated doors front and back, server LEDs blinking in the
 * shader, and a light bar per rack showing its inlet air temperature from the field.
 */
export function Racks({ layout, field }: { layout: HallLayout; field: React.RefObject<FieldInputs | null> }) {
  const m = materials();
  const mats = useMemo(() => rackMatrices(layout), [layout]);
  const bodyRef = useRef<THREE.InstancedMesh>(null);
  const doorRef = useRef<THREE.InstancedMesh>(null);
  const ledRef = useRef<THREE.InstancedMesh>(null);
  const barRef = useRef<THREE.InstancedMesh>(null);

  const doorMaterial = useMemo(() => {
    const map = perforatedTexture();
    return new THREE.MeshStandardNodeMaterial({ map, metalness: 0.65, roughness: 0.38 });
  }, []);
  const ledMaterial = useMemo(() => {
    const material = new THREE.MeshBasicNodeMaterial();
    const h = hash(instanceIndex);
    const blink = step(0.3, fract(time.mul(h.mul(2.5).add(0.3)).add(h.mul(17.0))));
    const hue = mix(vec3(0.1, 1.0, 0.45), vec3(0.2, 0.55, 1.0), step(0.8, h));
    material.colorNode = hue.mul(blink.mul(float(3.2)).add(0.25));
    return material;
  }, []);
  const barMaterial = useMemo(() => new THREE.MeshBasicNodeMaterial({ color: "#ffffff" }), []);

  useLayoutEffect(() => {
    const set = (mesh: THREE.InstancedMesh | null, list: THREE.Matrix4[]) => {
      if (!mesh) return;
      list.forEach((matrix, i) => mesh.setMatrixAt(i, matrix));
      mesh.instanceMatrix.needsUpdate = true;
      mesh.computeBoundingSphere();
    };
    set(bodyRef.current, mats.body);
    set(doorRef.current, mats.doors);
    set(ledRef.current, mats.leds);
    set(barRef.current, mats.bars);
    const bar = barRef.current;
    if (bar) {
      const c = new THREE.Color(0.2, 0.8, 1);
      mats.bars.forEach((_, i) => bar.setColorAt(i, c));
    }
  }, [mats]);

  const since = useRef(1);
  const colour = useMemo(() => new THREE.Color(), []);
  useFrame((_, dt) => {
    since.current += dt;
    const inputs = field.current;
    const bar = barRef.current;
    if (!inputs || !bar || since.current < 0.2) return;
    since.current = 0;
    mats.fronts.forEach(([x, z], i) => {
      const [r, g, b] = temperatureColour(fieldAt(inputs, x, z));
      bar.setColorAt(i, colour.setRGB(r * 2.6, g * 2.6, b * 2.6));
    });
    if (bar.instanceColor) bar.instanceColor.needsUpdate = true;
  });

  const n = mats.body.length;
  return (
    <group>
      <instancedMesh ref={bodyRef} args={[undefined, m.rackBody, n]} userData={{ kind: "racks" }}>
        <boxGeometry args={[RACK.w - 0.01, RACK.h, RACK.d]} />
      </instancedMesh>
      <instancedMesh ref={doorRef} args={[undefined, doorMaterial, n * 2]}>
        <planeGeometry args={[RACK.w - 0.04, RACK.h - 0.06]} />
      </instancedMesh>
      <instancedMesh ref={ledRef} args={[undefined, ledMaterial, n * LEDS_PER_RACK]}>
        <planeGeometry args={[0.018, 0.01]} />
      </instancedMesh>
      <instancedMesh ref={barRef} args={[undefined, barMaterial, n]}>
        <planeGeometry args={[RACK.w - 0.12, 0.022]} />
      </instancedMesh>
    </group>
  );
}

/** Hot-aisle containment: a smoked-glass roof and end doors over each hot aisle. */
export function Containment({ layout }: { layout: HallLayout }) {
  const m = materials();
  return (
    <group>
      {layout.hotAisles.map((a) => {
        const len = a.x1 - a.x0;
        const cx = (a.x0 + a.x1) / 2;
        return (
          <group key={a.z}>
            <mesh position={[cx, RACK.h + 0.02, a.z]} material={m.smokedGlass}>
              <boxGeometry args={[len, 0.02, HOT_AISLE_WIDTH]} />
            </mesh>
            {[a.x0, a.x1].map((x) => (
              <group key={x} position={[x, RACK.h / 2, a.z]}>
                <mesh material={m.smokedGlass}>
                  <boxGeometry args={[0.02, RACK.h, HOT_AISLE_WIDTH]} />
                </mesh>
                <mesh position={[0, RACK.h / 2 - 0.03, 0]} material={m.steel}>
                  <boxGeometry args={[0.05, 0.06, HOT_AISLE_WIDTH]} />
                </mesh>
                <mesh position={[0, 0, 0]} material={m.steel}>
                  <boxGeometry args={[0.05, RACK.h, 0.04]} />
                </mesh>
              </group>
            ))}
          </group>
        );
      })}
    </group>
  );
}

/** Cable trays over each row, busways over the hot aisles, and linear luminaires. */
export function Services({ layout }: { layout: HallLayout }) {
  const m = materials();
  return (
    <group>
      {layout.racks.map((r) => {
        const len = r.count * RACK.w;
        const cx = r.x0 + len / 2;
        return (
          <group key={`${r.z}`}>
            <mesh position={[cx, 2.6, r.z + r.front * 0.3]} material={m.galvanised}>
              <boxGeometry args={[len, 0.06, 0.3]} />
            </mesh>
            <mesh position={[cx, 2.78, r.z - r.front * 0.2]} material={m.trayYellow}>
              <boxGeometry args={[len, 0.08, 0.12]} />
            </mesh>
            <mesh position={[cx, 3.35, r.z + r.front * (RACK.d / 2 + 0.6)]} material={m.luminaire}>
              <boxGeometry args={[len, 0.03, 0.08]} />
            </mesh>
            <mesh position={[cx, 3.39, r.z + r.front * (RACK.d / 2 + 0.6)]} material={m.darkSteel}>
              <boxGeometry args={[len, 0.05, 0.14]} />
            </mesh>
          </group>
        );
      })}
      {layout.hotAisles.map((a) => (
        <mesh key={a.z} position={[(a.x0 + a.x1) / 2, 2.95, a.z]} material={m.busway}>
          <boxGeometry args={[a.x1 - a.x0, 0.16, 0.22]} />
        </mesh>
      ))}
    </group>
  );
}
