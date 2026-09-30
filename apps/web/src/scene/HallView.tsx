import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo } from "react";
import * as THREE from "three/webgpu";
import "./materials";
import { useSim } from "../live/store";
import { AlarmRings, CameraRig, SelectionRings } from "./Camera";
import { Effects, type EffectOptions } from "./Effects";
import { applyEnvironment } from "./environment";
import { AirField, Pipes, useFieldInputs } from "./hall/flows";
import { Containment, Racks, Services, Shell } from "./hall/Hall";
import { itLoadOf, layoutHall, type HallLayout, type HallWorld } from "./hall/layout";
import {
  CeilingCoils,
  CeilingDevice,
  CoolingBlock,
  FloorCable,
  PerimeterUnit,
  Pickable,
  Sensor,
  WallPanel,
} from "./hall/units";
import { createRenderer } from "./renderer";

export interface ViewOptions extends EffectOptions {
  airField: boolean;
}

/** Frames per second over the last half second, for the page's readout. */
export const frameStats = { fps: 0, frames: 0, since: 0 };

function FrameMeter() {
  useFrame(() => {
    const now = performance.now();
    frameStats.frames += 1;
    if (frameStats.since === 0) frameStats.since = now;
    if (now - frameStats.since >= 500) {
      frameStats.fps = (frameStats.frames * 1000) / (now - frameStats.since);
      frameStats.frames = 0;
      frameStats.since = now;
    }
  });
  return null;
}

function Environment() {
  const { gl, scene } = useThree();
  useEffect(() => {
    scene.background = new THREE.Color("#0b0f13");
    return applyEnvironment(gl as unknown as THREE.WebGPURenderer, scene);
  }, [gl, scene]);
  return null;
}

function Lights({ layout }: { layout: HallLayout }) {
  return (
    <group>
      <hemisphereLight args={["#dfe8f2", "#2a2f35", 0.45]} />
      <directionalLight position={[layout.width * 0.2, 12, layout.depth * 0.9]} intensity={0.75} color="#fff6ea" />
      {layout.racks
        .filter((_, i) => i % 2 === 0)
        .map((r) => (
          <pointLight
            key={r.z}
            position={[r.x0 + (r.count * 0.6) / 2, 3.2, r.z - 1.2]}
            intensity={9}
            distance={11}
            decay={1.6}
            color="#e8f0ff"
          />
        ))}
    </group>
  );
}

function Assets({ layout }: { layout: HallLayout }) {
  return (
    <group>
      {layout.assets.map((a) => {
        switch (a.kind) {
          case "fan-coil":
          case "perimeter":
            return <PerimeterUnit key={a.id} asset={a} />;
          case "ceiling-coils":
            return <CeilingCoils key={a.id} asset={a} />;
          case "cooling-block":
            return <CoolingBlock key={a.id} asset={a} />;
          case "wall-panel":
            return <WallPanel key={a.id} asset={a} />;
          case "sensor":
            return <Sensor key={a.id} asset={a} ceiling={layout.height - 0.2} />;
          case "ceiling-device":
            return <CeilingDevice key={a.id} asset={a} />;
          case "floor-cable":
            return <FloorCable key={a.id} asset={a} />;
          default:
            return null;
        }
      })}
    </group>
  );
}

function Hall({ layout, world, options }: { layout: HallLayout; world: HallWorld; options: ViewOptions }) {
  const field = useFieldInputs(layout, world.room.id);
  const it = itLoadOf(layout);
  const racks = <Racks layout={layout} field={field} />;
  return (
    <group>
      <Shell layout={layout} />
      {it ? <Pickable id={it.id}>{racks}</Pickable> : racks}
      <Containment layout={layout} />
      <Services layout={layout} />
      <Assets layout={layout} />
      <Pipes layout={layout} />
      <AirField layout={layout} field={field} visible={options.airField} />
      <SelectionRings layout={layout} />
      <AlarmRings layout={layout} />
    </group>
  );
}

/**
 * A data hall in 3D, generated from the World Model: the room, the assets placed in it, and
 * racks laid out from its hot aisles. Live values come from the frame store.
 */
export function HallView({ world, options }: { world: HallWorld; options: ViewOptions }) {
  const layout = useMemo(() => layoutHall(world), [world]);
  const select = useSim((s) => s.select);
  return (
    <Canvas
      className="hall-canvas"
      camera={{ fov: 40, near: 0.1, far: 300, position: [0, 20, 40] }}
      dpr={[1, 2]}
      gl={async ({ canvas }) => createRenderer(canvas)}
      onPointerMissed={() => select(null)}
      frameloop="always"
    >
      <Environment />
      <Lights layout={layout} />
      <Hall layout={layout} world={world} options={options} />
      <CameraRig layout={layout} />
      <Effects options={options} />
      <FrameMeter />
    </Canvas>
  );
}
