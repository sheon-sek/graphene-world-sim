import * as THREE from "three/webgpu";
import type { HallLayout } from "./hall/layout";
import { RACK } from "./hall/layout";

/**
 * Textures drawn at load time on a canvas, so the page needs no image files. The floor
 * texture also carries the hall's baked lighting: soft contact shadows under racks, units and
 * along walls, and pools of light under the luminaires, computed once from the layout.
 */

function canvas(w: number, h: number): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d");
  if (!ctx) throw new Error("no 2D canvas");
  return [c, ctx];
}

function texture(c: HTMLCanvasElement, srgb = true): THREE.CanvasTexture {
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = srgb ? THREE.SRGBColorSpace : THREE.NoColorSpace;
  t.anisotropy = 8;
  t.generateMipmaps = true;
  t.minFilter = THREE.LinearMipmapLinearFilter;
  return t;
}

const PX_PER_M = 48;
const TILE = 0.6;

/** Raised-floor tiles with perforated tiles in the cold aisles, and the baked shell lighting. */
export function floorTextures(layout: HallLayout): { map: THREE.CanvasTexture; rough: THREE.CanvasTexture } {
  const W = Math.round(layout.width * PX_PER_M);
  const H = Math.round(layout.depth * PX_PER_M);
  const [c, ctx] = canvas(W, H);
  const [rc, rctx] = canvas(W, H);
  const px = (m: number) => m * PX_PER_M;

  ctx.fillStyle = "#8d949b";
  ctx.fillRect(0, 0, W, H);
  rctx.fillStyle = "#8a8a8a";
  rctx.fillRect(0, 0, W, H);

  const coldAisle = (z: number) =>
    layout.racks.some((r) => {
      const face = r.z + r.front * (RACK.d / 2 + TILE / 2);
      return Math.abs(z - face) < TILE / 2;
    });

  for (let tz = 0; tz < layout.depth; tz += TILE) {
    for (let tx = 0; tx < layout.width; tx += TILE) {
      const shade = 138 + ((tx * 7 + tz * 13) % 5) * 2;
      ctx.fillStyle = `rgb(${shade},${shade + 5},${shade + 10})`;
      ctx.fillRect(px(tx) + 1, px(tz) + 1, px(TILE) - 2, px(TILE) - 2);
      if (coldAisle(tz + TILE / 2)) {
        ctx.fillStyle = "#4c5359";
        rctx.fillStyle = "#b5b5b5";
        for (let i = 1; i < 12; i++) {
          for (let j = 1; j < 12; j++) {
            const x = px(tx) + (i * px(TILE)) / 12;
            const y = px(tz) + (j * px(TILE)) / 12;
            ctx.fillRect(x - 0.9, y - 0.9, 1.8, 1.8);
            rctx.fillRect(x - 0.9, y - 0.9, 1.8, 1.8);
          }
        }
      }
    }
  }
  // Tile joints.
  ctx.strokeStyle = "rgba(40,45,50,0.55)";
  ctx.lineWidth = 1.2;
  for (let x = 0; x <= layout.width; x += TILE) {
    ctx.beginPath();
    ctx.moveTo(px(x), 0);
    ctx.lineTo(px(x), H);
    ctx.stroke();
  }
  for (let z = 0; z <= layout.depth; z += TILE) {
    ctx.beginPath();
    ctx.moveTo(0, px(z));
    ctx.lineTo(W, px(z));
    ctx.stroke();
  }
  // Cold-aisle safety lines.
  ctx.fillStyle = "rgba(230,190,40,0.85)";
  for (const r of layout.racks) {
    const edge = r.z + r.front * (RACK.d / 2 + 0.05);
    ctx.fillRect(px(r.x0), px(edge) - 1.5, px(r.count * RACK.w), 3);
  }

  // Baked light: pools under the luminaire lines, then contact shadows.
  ctx.globalCompositeOperation = "soft-light";
  for (const r of layout.racks) {
    const z = px(r.z + r.front * (RACK.d / 2 + TILE));
    const g = ctx.createLinearGradient(0, z - px(1.4), 0, z + px(1.4));
    g.addColorStop(0, "rgba(255,255,255,0)");
    g.addColorStop(0.5, "rgba(255,255,255,0.55)");
    g.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = g;
    ctx.fillRect(px(r.x0 - 0.5), z - px(1.4), px(r.count * RACK.w + 1), px(2.8));
  }
  ctx.globalCompositeOperation = "multiply";
  ctx.filter = `blur(${Math.round(px(0.22))}px)`;
  ctx.fillStyle = "rgba(20,24,28,0.75)";
  for (const r of layout.racks) {
    ctx.fillRect(px(r.x0 - 0.05), px(r.z - RACK.d / 2 - 0.05), px(r.count * RACK.w + 0.1), px(RACK.d + 0.1));
  }
  for (const a of layout.assets) {
    if (a.position[1] > 0.01 || a.size[0] === 0 || a.kind === "floor-cable") continue;
    const [x, , z] = a.position;
    const rotated = Math.abs(Math.sin(a.facing)) > 0.5;
    const w = rotated ? a.size[1] : a.size[0];
    const d = rotated ? a.size[0] : a.size[1];
    ctx.fillRect(px(x - w / 2 - 0.05), px(z - d / 2 - 0.05), px(w + 0.1), px(d + 0.1));
  }
  ctx.filter = `blur(${Math.round(px(0.5))}px)`;
  ctx.fillStyle = "rgba(40,44,50,0.5)";
  ctx.fillRect(0, 0, W, px(0.35));
  ctx.fillRect(W - px(0.35), 0, px(0.35), H);
  ctx.filter = "none";
  ctx.globalCompositeOperation = "source-over";
  return { map: texture(c), rough: texture(rc, false) };
}

/** Perforated steel for rack doors: bright where the steel is, dark holes. */
export function perforatedTexture(): THREE.CanvasTexture {
  const [c, ctx] = canvas(256, 512);
  ctx.fillStyle = "#1d2126";
  ctx.fillRect(0, 0, 256, 512);
  ctx.fillStyle = "#07090b";
  for (let y = 6; y < 506; y += 7) {
    for (let x = 6 + ((y / 7) % 2) * 3.5; x < 250; x += 7) {
      ctx.beginPath();
      ctx.arc(x, y, 2.3, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.fillStyle = "#2a3037";
  ctx.fillRect(0, 0, 256, 10);
  ctx.fillRect(0, 502, 256, 10);
  ctx.fillRect(0, 0, 8, 512);
  ctx.fillRect(248, 0, 8, 512);
  ctx.fillStyle = "#8f98a3";
  ctx.fillRect(226, 230, 10, 52);
  return texture(c);
}

/** Horizontal louvres for grilles: alternating light blade and dark gap. */
export function louvreTexture(blades = 24): THREE.CanvasTexture {
  const [c, ctx] = canvas(256, 256);
  const pitch = 256 / blades;
  for (let i = 0; i < blades; i++) {
    const g = ctx.createLinearGradient(0, i * pitch, 0, (i + 1) * pitch);
    g.addColorStop(0, "#d9dde1");
    g.addColorStop(0.55, "#9ba2a9");
    g.addColorStop(0.6, "#15181b");
    g.addColorStop(1, "#1f2327");
    ctx.fillStyle = g;
    ctx.fillRect(0, i * pitch, 256, pitch);
  }
  return texture(c);
}

/** A small status display: two numbers on a dark glass panel. Redrawn when values change. */
export class DisplayTexture {
  readonly texture: THREE.CanvasTexture;
  private readonly ctx: CanvasRenderingContext2D;
  private last = "";

  constructor() {
    const [c, ctx] = canvas(256, 128);
    this.ctx = ctx;
    this.texture = texture(c);
  }

  draw(title: string, lines: string[], alarm: boolean): void {
    const key = title + lines.join("|") + alarm;
    if (key === this.last) return;
    this.last = key;
    const ctx = this.ctx;
    ctx.fillStyle = alarm ? "#2a0707" : "#04121a";
    ctx.fillRect(0, 0, 256, 128);
    ctx.fillStyle = alarm ? "#ff5a4a" : "#6fe3ff";
    ctx.font = "600 22px ui-monospace, monospace";
    ctx.fillText(title, 14, 30);
    ctx.font = "500 20px ui-monospace, monospace";
    ctx.fillStyle = alarm ? "#ffb3a8" : "#d6f6ff";
    lines.forEach((l, i) => ctx.fillText(l, 14, 62 + i * 26));
    this.texture.needsUpdate = true;
  }
}
