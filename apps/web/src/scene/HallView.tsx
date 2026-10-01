import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
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
import { QUALITY, SLOW_FPS, SLOW_SECONDS, type Quality } from "./quality";
import { createRenderer } from "./renderer";

export interface ViewOptions extends EffectOptions {
  airField: boolean;
}

/** Frames per second over the last half second, for the page's readout. */
export const frameStats = { fps: 0, frames: 0, since: 0 };

function FrameMeter({ onSlow }: { onSlow?: (fps: number) => void }) {
  const slowSince = useRef<number | null>(null);
  const started = useRef(performance.now());
  useFrame(() => {
    const now = performance.now();
    frameStats.frames += 1;
    if (frameStats.since === 0) frameStats.since = now;
    if (now - frameStats.since >= 500) {
      frameStats.fps = (frameStats.frames * 1000) / (now - frameStats.since);
      frameStats.frames = 0;
      frameStats.since = now;
      // Shaders compile during the first seconds; judge the frame rate only after them.
      if (!onSlow || now - started.current < 3000) return;
      if (frameStats.fps >= SLOW_FPS) slowSince.current = null;
      else if (slowSince.current === null) slowSince.current = now;
      else if (now - slowSince.current >= SLOW_SECONDS * 1000) {
        slowSince.current = null;
        onSlow(frameStats.fps);
      }
    }
  });
  return null;
}

/** Draws at most `fps` frames a second, on a canvas whose frame loop is on demand. */
function FrameLimiter({ fps }: { fps: number }) {
  const invalidate = useThree((s) => s.invalidate);
  useEffect(() => {
    const id = window.setInterval(() => invalidate(), 1000 / fps);
    return () => window.clearInterval(id);
  }, [fps, invalidate]);
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

function Lights({ layout, rackLights }: { layout: HallLayout; rackLights: boolean }) {
  return (
    <group>
      {/* Without the rack lights, a brighter sky light keeps the aisles readable. */}
      <hemisphereLight args={["#dfe8f2", "#2a2f35", rackLights ? 0.45 : 1.1]} />
      <directionalLight position={[layout.width * 0.2, 12, layout.depth * 0.9]} intensity={0.75} color="#fff6ea" />
      {rackLights &&
        layout.racks
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
 * racks laid out from its hot aisles. Live values come from the frame store. `quality` sets
 * what the view asks of the GPU (see `quality.ts`); `onSlow` is told when the frame rate stays
 * low, so the page can step down.
 */
export function HallView({
  world,
  options,
  quality = "high",
  onSlow,
}: {
  world: HallWorld;
  options: ViewOptions;
  quality?: Quality;
  onSlow?: (fps: number) => void;
}) {
  const layout = useMemo(() => layoutHall(world), [world]);
  const select = useSim((s) => s.select);
  const q = QUALITY[quality];
  const effects = {
    ambientOcclusion: options.ambientOcclusion && q.ambientOcclusion,
    bloom: options.bloom && q.bloom,
  };
  return (
    <Canvas
      // Antialiasing is fixed when the renderer is made, so a new quality makes a new one.
      key={quality}
      className="hall-canvas"
      camera={{ fov: 40, near: 0.25, far: 300, position: [0, 20, 40] }}
      dpr={q.dpr}
      gl={async ({ canvas }) => createRenderer(canvas, { antialias: q.antialias })}
      onPointerMissed={(e) => {
        // A click on empty floor goes back to the overview; the end of a drag does not.
        if (e.type === "click") select(null);
      }}
      frameloop={q.maxFps > 0 ? "demand" : "always"}
    >
      <Environment />
      <Lights layout={layout} rackLights={q.rackLights} />
      <Hall layout={layout} world={world} options={options} />
      <CameraRig layout={layout} />
      {(effects.ambientOcclusion || effects.bloom) && <Effects options={effects} />}
      {q.maxFps > 0 && <FrameLimiter fps={q.maxFps} />}
      <FrameMeter onSlow={quality === "high" ? onSlow : undefined} />
    </Canvas>
  );
}
