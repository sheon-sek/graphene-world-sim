import * as THREE from "three/webgpu";

/**
 * An HDR environment for image-based lighting, generated rather than downloaded: a dim room
 * with bright linear ceiling luminaires and faint cool wall panels, the light a data hall
 * reflects. Emissive values above 1 keep it high dynamic range, so metal and glass pick up
 * crisp highlights. The page loads no external assets.
 */
export function hallEnvironmentScene(): THREE.Scene {
  const scene = new THREE.Scene();
  const room = new THREE.Mesh(
    new THREE.BoxGeometry(40, 8, 40),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(0.035, 0.04, 0.05), side: THREE.BackSide }),
  );
  room.position.y = 3;
  scene.add(room);

  const strip = new THREE.MeshBasicMaterial({ color: new THREE.Color(9, 9.4, 10) });
  const stripGeometry = new THREE.BoxGeometry(0.35, 0.05, 14);
  for (let i = -3; i <= 3; i++) {
    const m = new THREE.Mesh(stripGeometry, strip);
    m.position.set(i * 4.5, 6.9, 0);
    scene.add(m);
  }

  const panel = new THREE.MeshBasicMaterial({ color: new THREE.Color(0.25, 0.55, 0.9) });
  for (const [x, z, ry] of [
    [-19.9, 0, Math.PI / 2],
    [19.9, 0, -Math.PI / 2],
  ] as const) {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(24, 1.2), panel);
    m.position.set(x, 1.5, z);
    m.rotation.y = ry;
    scene.add(m);
  }

  const warm = new THREE.MeshBasicMaterial({ color: new THREE.Color(2.4, 1.6, 0.9) });
  const w = new THREE.Mesh(new THREE.PlaneGeometry(10, 3), warm);
  w.position.set(0, 3, -19.9);
  scene.add(w);
  return scene;
}

export function applyEnvironment(renderer: THREE.WebGPURenderer, scene: THREE.Scene): () => void {
  const pmrem = new THREE.PMREMGenerator(renderer);
  const source = hallEnvironmentScene();
  const target = pmrem.fromScene(source, 0.035);
  scene.environment = target.texture;
  scene.environmentIntensity = 0.9;
  return () => {
    scene.environment = null;
    target.dispose();
    pmrem.dispose();
    source.traverse((o) => {
      if (o instanceof THREE.Mesh) {
        o.geometry.dispose();
        (o.material as THREE.Material).dispose();
      }
    });
  };
}
