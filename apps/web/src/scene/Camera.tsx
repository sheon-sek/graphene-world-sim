import { useFrame, useThree } from "@react-three/fiber";
import CameraControls from "camera-controls";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three/webgpu";
import { useSim } from "../live/store";
import type { HallLayout, SceneAsset } from "./hall/layout";
import { RACK } from "./hall/layout";

CameraControls.install({
  THREE: {
    Vector2: THREE.Vector2,
    Vector3: THREE.Vector3,
    Vector4: THREE.Vector4,
    Quaternion: THREE.Quaternion,
    Matrix4: THREE.Matrix4,
    Spherical: THREE.Spherical,
    Box3: THREE.Box3,
    Sphere: THREE.Sphere,
    Raycaster: THREE.Raycaster,
  },
});

/** Where the camera looks at an asset from: in front of it, raised, looking slightly down. */
export function viewOf(asset: SceneAsset, layout: HallLayout): { eye: THREE.Vector3; target: THREE.Vector3 } {
  if (asset.kind === "it-load") {
    const row = layout.racks[Math.floor(layout.racks.length / 2)];
    const target = new THREE.Vector3(row ? row.x0 + (row.count * RACK.w) / 2 : asset.position[0], 1.1, row?.z ?? 0);
    return { eye: target.clone().add(new THREE.Vector3(-4.5, 3.2, 6.5)), target };
  }
  const [w, , h] = asset.size;
  const target = new THREE.Vector3(asset.position[0], asset.position[1] + Math.max(h, 0.3) * 0.55, asset.position[2]);
  const reach = Math.max(2.8, w * 1.6 + 1.5);
  const out = new THREE.Vector3(Math.sin(asset.facing), 0, Math.cos(asset.facing));
  if (asset.kind === "ceiling-coils") out.set(0.35, 0, 1).normalize();
  const side = new THREE.Vector3(out.z, 0, -out.x).multiplyScalar(reach * 0.35);
  const eye = target.clone().add(out.multiplyScalar(reach)).add(side).add(new THREE.Vector3(0, reach * 0.45, 0));
  return { eye, target };
}

export function overview(layout: HallLayout): { eye: THREE.Vector3; target: THREE.Vector3 } {
  const target = new THREE.Vector3(layout.width * 0.55, 0.6, layout.depth * 0.5);
  const eye = new THREE.Vector3(-layout.width * 0.3, layout.depth * 0.5, layout.depth * 1.12);
  return { eye, target };
}

/**
 * Orbit, pan and zoom around the hall, and fly to the selected asset.
 *
 * Left drag orbits; right drag, middle drag or Shift with left drag pans; the wheel zooms
 * towards the cursor; the arrow keys pan. The controls live as long as the canvas: the
 * overview is set when the hall changes, and a flight starts only when the selection changes.
 */
export function CameraRig({ layout }: { layout: HallLayout }) {
  const { camera, gl } = useThree();
  const controls = useMemo(() => new CameraControls(camera, gl.domElement), [camera, gl]);
  const selected = useSim((s) => s.selected);
  const flownTo = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    controls.minDistance = 1.5;
    controls.maxDistance = 70;
    controls.maxPolarAngle = Math.PI * 0.48;
    controls.dollyToCursor = true;
    controls.smoothTime = 0.45;
    controls.draggingSmoothTime = 0.08;
    const A = CameraControls.ACTION;
    controls.mouseButtons.left = A.ROTATE;
    controls.mouseButtons.middle = A.TRUCK;
    controls.mouseButtons.right = A.TRUCK;
    controls.mouseButtons.wheel = A.DOLLY;
    const element = gl.domElement;
    // Shift turns the left button into pan, for trackpads and one-button mice.
    const shift = (e: KeyboardEvent) => {
      controls.mouseButtons.left = e.shiftKey ? A.TRUCK : A.ROTATE;
    };
    const keys = (e: KeyboardEvent) => {
      const step = Math.max(0.5, controls.distance * 0.05);
      const target = e.target as HTMLElement | null;
      if (target && /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) return;
      const moves: Record<string, [number, number]> = {
        ArrowLeft: [-step, 0],
        ArrowRight: [step, 0],
        ArrowUp: [0, step],
        ArrowDown: [0, -step],
      };
      const move = moves[e.key];
      if (!move) return;
      e.preventDefault();
      void controls.truck(move[0], 0, true);
      void controls.forward(move[1], true);
    };
    const menu = (e: Event) => e.preventDefault();
    element.addEventListener("contextmenu", menu);
    window.addEventListener("keydown", shift);
    window.addEventListener("keyup", shift);
    element.tabIndex = 0;
    if (window.gws) window.gws.camera = controls; // for end-to-end tests and the console
    element.addEventListener("keydown", keys);
    return () => {
      window.removeEventListener("keydown", shift);
      window.removeEventListener("keyup", shift);
      element.removeEventListener("keydown", keys);
      element.removeEventListener("contextmenu", menu);
      controls.dispose();
    };
  }, [controls, gl]);

  useEffect(() => {
    const { eye, target } = overview(layout);
    void controls.setLookAt(eye.x, eye.y, eye.z, target.x, target.y, target.z, false);
    flownTo.current = undefined;
  }, [controls, layout]);

  useEffect(() => {
    if (flownTo.current === undefined) {
      // The first selection seen is where the page opened; the overview stays.
      flownTo.current = selected;
      return;
    }
    if (flownTo.current === selected) return;
    flownTo.current = selected;
    const asset = layout.assets.find((a) => a.id === selected);
    const { eye, target } = asset ? viewOf(asset, layout) : overview(layout);
    void controls.setLookAt(eye.x, eye.y, eye.z, target.x, target.y, target.z, true);
  }, [selected, layout, controls]);

  const invalidate = useThree((s) => s.invalidate);
  useFrame((_, dt) => {
    // With the frame loop on demand, keep drawing while the camera is still moving.
    if (controls.update(dt)) invalidate();
  }, -1);
  return null;
}

/** Glowing floor rings under the hovered and the selected asset. */
export function SelectionRings({ layout }: { layout: HallLayout }) {
  const selected = useSim((s) => s.selected);
  const hovered = useSim((s) => s.hovered);
  const selectedMaterial = useMemo(
    () => new THREE.MeshBasicNodeMaterial({ color: new THREE.Color(0.3, 2.4, 3.2), transparent: true, opacity: 0.95 }),
    [],
  );
  const hoverMaterial = useMemo(
    () => new THREE.MeshBasicNodeMaterial({ color: new THREE.Color(0.8, 1.2, 1.5), transparent: true, opacity: 0.6 }),
    [],
  );
  const ring = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => {
    if (ring.current) ring.current.scale.setScalar(1 + 0.04 * Math.sin(clock.elapsedTime * 3));
  });
  const place = (id: string | null) => {
    const a = layout.assets.find((x) => x.id === id);
    if (!a || a.kind === "it-load") return null;
    const radius = Math.max(0.35, Math.hypot(a.size[0], a.size[1]) / 2 + 0.25);
    return { a, radius };
  };
  const s = place(selected);
  const h = hovered !== selected ? place(hovered) : null;
  return (
    <group>
      {s && (
        <mesh ref={ring} material={selectedMaterial} rotation-x={-Math.PI / 2} position={[s.a.position[0], 0.012, s.a.position[2]]}>
          <ringGeometry args={[s.radius, s.radius + 0.06, 64]} />
        </mesh>
      )}
      {h && (
        <mesh material={hoverMaterial} rotation-x={-Math.PI / 2} position={[h.a.position[0], 0.011, h.a.position[2]]}>
          <ringGeometry args={[h.radius, h.radius + 0.035, 64]} />
        </mesh>
      )}
    </group>
  );
}

function AlarmRing({ asset }: { asset: SceneAsset }) {
  const material = useMemo(
    () => new THREE.MeshBasicNodeMaterial({ color: new THREE.Color(5, 0.3, 0.2), transparent: true, opacity: 0.9 }),
    [],
  );
  const ring = useRef<THREE.Mesh>(null);
  const light = useRef<THREE.PointLight>(null);
  const radius = Math.max(0.6, Math.hypot(asset.size[0], asset.size[1]) / 2 + 0.5);
  useFrame(({ clock }) => {
    const k = (clock.elapsedTime * 0.7) % 1;
    if (ring.current) ring.current.scale.setScalar(1 + k * 2.2);
    material.opacity = 0.95 * (1 - k) ** 1.5;
    if (light.current) light.current.intensity = 14 * (0.55 + 0.45 * Math.sin(clock.elapsedTime * 6));
  });
  const y = asset.position[1] > 0.5 ? asset.position[1] - 0.2 : 0.013;
  const [x, , z] = asset.position;
  return (
    <group>
      <mesh ref={ring} material={material} rotation-x={-Math.PI / 2} position={[x, y, z]}>
        <ringGeometry args={[radius, radius + 0.1, 64]} />
      </mesh>
      <pointLight ref={light} position={[x, Math.max(asset.size[2], 1) + 0.6, z]} color="#ff3a24" distance={7} decay={1.5} />
    </group>
  );
}

/** Expanding red rings under every asset a fault is active on. */
export function AlarmRings({ layout }: { layout: HallLayout }) {
  const targets = useSim((s) => (s.frame?.faults ?? []).map((f) => f.target).sort().join("\n"));
  const assets = layout.assets.filter((a) => targets.split("\n").includes(a.id));
  return (
    <group>
      {assets.map((a) => (
        <AlarmRing key={a.id} asset={a} />
      ))}
    </group>
  );
}
