import { useSim } from "../live/store";
import { clock } from "./format";
import { lifecycle, useSession } from "./session";

const SPEEDS = [1, 5, 10, 30, 60, 0];

/** The simulation clock and the run, pause, step and speed controls. */
export function Lifecycle({ sid }: { sid: string }) {
  const info = useSession((s) => s.info);
  const t = useSim((s) => s.frame?.t ?? 0);
  const step = useSim((s) => s.frame?.step ?? 0);
  const running = info?.running ?? false;
  return (
    <div className="lifecycle">
      <span className="clock" data-testid="sim-clock" title={`step ${step}`}>
        {clock(t)}
      </span>
      {running ? (
        <button onClick={() => void lifecycle.pause(sid)} data-testid="pause">
          Pause
        </button>
      ) : (
        <button className="primary" onClick={() => void lifecycle.run(sid)} data-testid="run">
          Run
        </button>
      )}
      <button disabled={running} onClick={() => void lifecycle.step(sid, 1)} title="One step">
        Step
      </button>
      <button disabled={running} onClick={() => void lifecycle.step(sid, 60)} title="Sixty steps">
        +60
      </button>
      <select
        aria-label="Speed"
        value={info?.speed ?? 1}
        onChange={(e) => void lifecycle.speed(sid, Number(e.target.value))}
      >
        {SPEEDS.map((s) => (
          <option key={s} value={s}>
            {s === 0 ? "Max" : `${s}×`}
          </option>
        ))}
      </select>
    </div>
  );
}
