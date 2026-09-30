import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo } from "react";
import * as THREE from "three/webgpu";
import { unpackRGBToNormal, packNormalToRGB, mrt, normalView, output, pass, sample, vec4 } from "three/tsl";
import { bloom } from "three/addons/tsl/display/BloomNode.js";
import { ao } from "three/addons/tsl/display/GTAONode.js";

type ShaderNode = ReturnType<typeof vec4>;

export interface EffectOptions {
  ambientOcclusion: boolean;
  bloom: boolean;
}

/**
 * Post-processing in TSL, so it runs on the WebGPU and WebGL2 backends alike: a scene pass
 * with view normals, ground-truth ambient occlusion at half resolution, and bloom on the
 * emissive highlights (LEDs, luminaires, status beacons, flow pulses). Tone mapping and the
 * output colour transform are applied by the render pipeline.
 */
export function Effects({ options }: { options: EffectOptions }) {
  const { gl, scene, camera } = useThree();
  const pipeline = useMemo(() => {
    const renderer = gl as unknown as THREE.WebGPURenderer;
    const pipeline = new THREE.RenderPipeline(renderer);
    const scenePass = pass(scene, camera);
    let colour = scenePass.getTextureNode("output");
    let result = vec4(colour) as unknown as ShaderNode;
    if (options.ambientOcclusion) {
      scenePass.setMRT(mrt({ output, normal: packNormalToRGB(normalView) }));
      colour = scenePass.getTextureNode("output");
      const normals = scenePass.getTextureNode("normal");
      const depth = scenePass.getTextureNode("depth");
      const occlusion = ao(
        depth,
        sample((uv) => unpackRGBToNormal(normals.sample(uv))),
        camera,
      );
      occlusion.resolutionScale = 0.5;
      occlusion.radius.value = 0.45;
      occlusion.thickness.value = 0.6;
      occlusion.scale.value = 1.15;
      result = vec4(colour.rgb.mul(occlusion.getTextureNode().r), colour.a) as unknown as ShaderNode;
    }
    if (options.bloom) {
      result = result.add(bloom(result, 0.55, 0.4, 0.82)) as unknown as ShaderNode;
    }
    pipeline.outputNode = result;
    return pipeline;
  }, [gl, scene, camera, options.ambientOcclusion, options.bloom]);

  useEffect(() => () => pipeline.dispose(), [pipeline]);
  useFrame(() => {
    pipeline.render();
  }, 1);
  return null;
}
