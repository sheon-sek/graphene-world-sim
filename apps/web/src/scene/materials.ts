import { extend, type ThreeToJSXElements } from "@react-three/fiber";
import * as THREE from "three/webgpu";

declare module "@react-three/fiber" {
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  interface ThreeElements extends ThreeToJSXElements<typeof THREE> {}
}

extend(THREE as unknown as Parameters<typeof extend>[0]);

/** Physically based materials shared across the hall. Created once, on first use. */
function standard(params: THREE.MeshStandardNodeMaterialParameters): THREE.MeshStandardNodeMaterial {
  return new THREE.MeshStandardNodeMaterial(params);
}

let cache: ReturnType<typeof build> | null = null;

function build() {
  return {
    powderCoat: standard({ color: "#dfe3e6", metalness: 0.15, roughness: 0.48 }),
    powderDark: standard({ color: "#3a4047", metalness: 0.2, roughness: 0.5 }),
    rackBody: standard({ color: "#16191d", metalness: 0.55, roughness: 0.42 }),
    steel: standard({ color: "#b8bfc6", metalness: 0.85, roughness: 0.28 }),
    galvanised: standard({ color: "#9aa3aa", metalness: 0.8, roughness: 0.45 }),
    darkSteel: standard({ color: "#2b3036", metalness: 0.7, roughness: 0.35 }),
    rubber: standard({ color: "#0d0f11", metalness: 0, roughness: 0.9 }),
    fanBlade: standard({ color: "#1f2429", metalness: 0.3, roughness: 0.4, side: THREE.DoubleSide }),
    copper: standard({ color: "#b8734a", metalness: 0.95, roughness: 0.3 }),
    wall: standard({ color: "#aab1b8", metalness: 0, roughness: 0.9 }),
    wallDark: standard({ color: "#474e56", metalness: 0.1, roughness: 0.7 }),
    trayYellow: standard({ color: "#c99a16", metalness: 0.1, roughness: 0.55 }),
    busway: standard({ color: "#7d858d", metalness: 0.75, roughness: 0.35 }),
    smokedGlass: standard({
      color: "#9fb3c2",
      metalness: 0,
      roughness: 0.08,
      transparent: true,
      opacity: 0.18,
      depthWrite: false,
      side: THREE.DoubleSide,
    }),
    luminaire: new THREE.MeshBasicNodeMaterial({ color: new THREE.Color(6, 6.3, 6.8) }),
    ghost: standard({ color: "#8a939c", metalness: 0.1, roughness: 0.7, transparent: true, opacity: 0.55 }),
  };
}

export function materials() {
  cache ??= build();
  return cache;
}

export type Materials = ReturnType<typeof build>;
