import { describe as suite, expect, it } from "vitest";
import { describe, elapsed } from "./BuildProgress";

const build = {
  partition: "P_1",
  assets: 121,
  phase: "compiling",
  elapsed_s: 125,
  done: 30,
  total: 120,
  memory_gb: 8.2,
  memory_limit_gb: 15.7,
  memory_needed_gb: 8.9,
};

suite("build progress", () => {
  it("says the step, the files compiled and the memory", () => {
    const d = describe(build);
    expect(d.title).toBe("Compiling C code 30 of 120 files");
    expect(d.fraction).toBe(0.25);
    expect(d.detail).toBe("121 assets · 2:05 elapsed · memory 8.2 GB of 15.7 GB, needs about 8.9 GB");
    expect(describe({ ...build, phase: "translating", memory_gb: null }).fraction).toBeNull();
    expect(elapsed(59)).toBe("0:59");
  });
});
