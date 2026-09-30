import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import { stateFlag, stateNumber, type Frame } from "../live/frames";
import { useSim } from "../live/store";

/**
 * Frame values for the render loop. Frames arrive a few times a second; the scene eases each
 * value towards the latest frame so motion stays smooth at the display's frame rate. Values
 * are read from the store inside `useFrame`, never through React state.
 */
export class Eased {
  value: number;
  constructor(
    initial: number,
    private readonly tau: number,
  ) {
    this.value = initial;
  }

  step(target: number, dt: number): number {
    if (!Number.isFinite(target)) return this.value;
    this.value += (target - this.value) * (1 - Math.exp(-dt / this.tau));
    return this.value;
  }
}

export function currentFrame(): Frame | null {
  return useSim.getState().frame;
}

/** Ease a number read from each frame; `ref.current` holds the eased value. */
export function useEasedValue(read: (frame: Frame | null) => number, tau = 0.8, initial = 0) {
  const eased = useRef(new Eased(initial, tau));
  useFrame((_, dt) => {
    eased.current.step(read(currentFrame()), Math.min(dt, 0.1));
  });
  return eased;
}

export { stateFlag, stateNumber };

export interface UnitLive {
  /** Air flow as a fraction of design. */
  flow: number;
  supplyC: number;
  returnC: number;
  chwSupplyC: number;
  chwReturnC: number;
  chwFlow: number;
  tripped: boolean;
  energised: boolean;
  simulated: boolean;
}

export function unitLive(frame: Frame | null, id: string, designAirFlow: number): UnitLive {
  const s = frame?.state[id];
  const n = (k: string, f = 0) => stateNumber(frame, id, k, f);
  return {
    flow: designAirFlow > 0 ? n("mAir_flow") / designAirFlow : 0,
    supplyC: n("TSupAir", 293.15) - 273.15,
    returnC: n("TRetAir", 300.15) - 273.15,
    chwSupplyC: n("TChwEnt", 287.15) - 273.15,
    chwReturnC: n("TChwLvg", 293.15) - 273.15,
    chwFlow: n("mChw_flow"),
    tripped: stateFlag(frame, id, "tripped"),
    energised: s ? stateFlag(frame, id, "energised") : false,
    simulated: s !== undefined,
  };
}
