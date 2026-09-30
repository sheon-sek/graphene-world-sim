/** Which backend renders the 3D view. Kept apart from the renderer so pages can read it
 * without loading three.js. */
export interface RendererInfo {
  backend: "WebGPU" | "WebGL2";
}

export const rendererInfo: RendererInfo = { backend: "WebGL2" };

/** Reload on the WebGL2 backend, once, when the WebGPU backend fails while rendering. */
export function fallBackToWebGL(error: unknown): boolean {
  const url = new URL(window.location.href);
  if (rendererInfo.backend !== "WebGPU" || url.searchParams.has("webgl")) return false;
  console.warn("WebGPU rendering failed, reloading on WebGL2", error);
  url.searchParams.set("webgl", "1");
  window.location.replace(url.toString());
  return true;
}
