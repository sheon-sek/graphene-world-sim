import { display, guessUnit, shortName } from "./format";

describe("display", () => {
  it("shows SI state values in operator units", () => {
    expect(display("TAir", 298.15)).toEqual({ value: "25.0", unit: "°C" });
    expect(display("Q", 150000)).toEqual({ value: "150.0", unit: "kW" });
    expect(display("tripped", true)).toEqual({ value: "true", unit: "" });
    expect(guessUnit("mChw_flow")).toBe("kg/s");
    expect(guessUnit("THDV")).toBeNull();
    expect(shortName("FCU/L1_FCU1")).toBe("L1_FCU1");
  });
});
