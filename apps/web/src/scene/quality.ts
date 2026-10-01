/**
 * How much the 3D view asks of the GPU. `high` is the full look ADR-0004 describes: up to 2x
 * pixel density, antialiasing, ambient occlusion, bloom, a point light per rack pair and 60
 * frames a second. `low` is for integrated graphics: native pixel density, no antialiasing or
 * post-processing, two lights, and at most 30 frames a second. Both draw the same scene, so
 * nothing an operator reads (colours, flows, alarms) is lost.
 *
 * The choice comes from `?quality=high|low`, then the one saved in this browser, then the GPU
 * the browser reports: integrated and software renderers start on `low`.
 */
export type Quality = "high" | "low";

export interface QualitySettings {
  /** Device pixel ratio range for the canvas. */
  dpr: [number, number];
  antialias: boolean;
  ambientOcclusion: boolean;
  bloom: boolean;
  /** Point lights over the rack rows. */
  rackLights: boolean;
  /** Frames a second the view draws at most; 0 is the display's own rate. */
  maxFps: number;
}

export const QUALITY: Record<Quality, QualitySettings> = {
  high: { dpr: [1, 2], antialias: true, ambientOcclusion: true, bloom: true, rackLights: true, maxFps: 0 },
  low: { dpr: [1, 1], antialias: false, ambientOcclusion: false, bloom: false, rackLights: false, maxFps: 30 },
};

const KEY = "gws.quality";

/** Renderers that share memory with the CPU, or run in software. */
const INTEGRATED =
  /intel|iris|uhd graphics|hd graphics|swiftshader|llvmpipe|softpipe|software|basic render|mali|adreno|powervr|videocore|radeon\(tm\) graphics|radeon graphics|vega \d+ graphics/i;

export function isIntegrated(renderer: string): boolean {
  return INTEGRATED.test(renderer);
}

/** The GPU the browser reports, where it reports one. */
export function gpuName(): string | null {
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    if (!gl) return null;
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    const name = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    return typeof name === "string" ? name : null;
  } catch {
    return null;
  }
}

function parse(value: string | null | undefined): Quality | null {
  return value === "high" || value === "low" ? value : null;
}

export function savedQuality(): Quality | null {
  try {
    return parse(localStorage.getItem(KEY));
  } catch {
    return null;
  }
}

export function saveQuality(quality: Quality): void {
  try {
    localStorage.setItem(KEY, quality);
  } catch {
    /* a private window keeps the choice for this page only */
  }
}

/** The quality to start with, and why. */
export function initialQuality(): { quality: Quality; reason: string } {
  const asked = parse(new URLSearchParams(globalThis.location?.search ?? "").get("quality"));
  if (asked) return { quality: asked, reason: "set in the address" };
  const saved = savedQuality();
  if (saved) return { quality: saved, reason: "your choice" };
  const gpu = gpuName();
  if (gpu === null) return { quality: "low", reason: "the GPU is unknown" };
  return isIntegrated(gpu) ? { quality: "low", reason: `integrated GPU (${gpu})` } : { quality: "high", reason: gpu };
}

/** Below this many frames a second, held for `SLOW_SECONDS`, the high view steps down to low. */
export const SLOW_FPS = 24;
export const SLOW_SECONDS = 4;
