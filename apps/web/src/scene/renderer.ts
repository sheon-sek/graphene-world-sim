import * as THREE from "three/webgpu";
import { rendererInfo } from "./backend";

export { fallBackToWebGL, rendererInfo, type RendererInfo } from "./backend";

/**
 * The renderer ADR-0004 decides: three.js's WebGPU renderer, which runs on its WebGL2 backend
 * where the browser has no WebGPU. Everything drawn uses node materials and TSL, so the same
 * scene and post-processing run on both backends.
 */
export async function createRenderer(canvas: unknown): Promise<THREE.WebGPURenderer> {
  const forceWebGL = new URLSearchParams(globalThis.location?.search ?? "").has("webgl");
  const make = (webgl: boolean) =>
    new THREE.WebGPURenderer({
      canvas: canvas as HTMLCanvasElement,
      antialias: true,
      forceWebGL: webgl,
      powerPreference: "high-performance",
    });
  let renderer = make(forceWebGL);
  try {
    await renderer.init();
  } catch (e) {
    if (forceWebGL) throw e;
    console.warn("WebGPU failed to start, using WebGL2", e);
    renderer.dispose();
    renderer = make(true);
    await renderer.init();
  }
  const backend = (renderer as unknown as { backend: { isWebGPUBackend?: boolean } }).backend;
  rendererInfo.backend = backend.isWebGPUBackend ? "WebGPU" : "WebGL2";
  renderer.toneMapping = THREE.AgXToneMapping;
  renderer.toneMappingExposure = 1.05;
  return renderer;
}

