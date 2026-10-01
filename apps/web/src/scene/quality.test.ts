import { initialQuality, isIntegrated } from "./quality";

describe("3D quality", () => {
  it("treats integrated and software renderers as low-end", () => {
    for (const gpu of [
      "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
      "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
      "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
      "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)",
      "llvmpipe (LLVM 15.0.7, 256 bits)",
      "ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ])
      expect(isIntegrated(gpu)).toBe(true);
    for (const gpu of [
      "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
      "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
      "Apple M2",
    ])
      expect(isIntegrated(gpu)).toBe(false);
  });

  it("takes the quality from the address first", () => {
    window.history.replaceState(null, "", "/?quality=low");
    expect(initialQuality()).toEqual({ quality: "low", reason: "set in the address" });
    window.history.replaceState(null, "", "/");
  });
});
