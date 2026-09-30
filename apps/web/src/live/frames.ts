/**
 * Runtime frames and where they come from.
 *
 * A frame is what the runtime publishes after each step (ADR-0002): the true state of every
 * simulated asset, the measured points, and the active faults. The web app never computes
 * behaviour; it only shows frames. A `FrameSource` is either the live WebSocket stream of a
 * runtime session or a recorded trajectory played back at a chosen speed.
 */

export type Scalar = number | boolean | string | null;

export interface PointValue {
  value: Scalar;
  quality: string;
  t: number;
  reason?: string;
}

export interface ActiveFault {
  id: string;
  target: string;
  mode: string;
  severity?: number;
  [key: string]: unknown;
}

export interface FrameEvent {
  kind: string;
  target?: string;
  mode?: string;
}

export interface Frame {
  t: number;
  step: number;
  state: Record<string, Record<string, Scalar>>;
  points: Record<string, PointValue>;
  faults: ActiveFault[];
  /** Commands, faults and resets applied just before this frame (recordings only). */
  events?: FrameEvent[];
}

export type Listener = (frame: Frame) => void;

export interface FrameSource {
  readonly kind: "live" | "replay";
  subscribe(listener: Listener): () => void;
  close(): void;
}

/** A number from an asset's true state, or `fallback` when it is missing or not numeric. */
export function stateNumber(frame: Frame | null, asset: string, signal: string, fallback = 0): number {
  const value = frame?.state[asset]?.[signal];
  if (typeof value === "number") return value;
  if (typeof value === "boolean") return value ? 1 : 0;
  return fallback;
}

export function stateFlag(frame: Frame | null, asset: string, signal: string): boolean {
  return Boolean(frame?.state[asset]?.[signal]);
}

export const kelvinToCelsius = (k: number): number => k - 273.15;

class Emitter {
  private listeners = new Set<Listener>();
  protected last: Frame | null = null;

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    if (this.last) listener(this.last);
    return () => this.listeners.delete(listener);
  }

  protected emit(frame: Frame): void {
    this.last = frame;
    for (const listener of this.listeners) listener(frame);
  }
}

/**
 * A recorded trajectory played back in simulation time. `advance(wallSeconds)` moves the
 * playhead and emits the frame it lands on; `start()` drives it from the browser clock. Tests
 * call `advance` directly, so no test waits on real time.
 */
export class ReplaySource extends Emitter implements FrameSource {
  readonly kind = "replay" as const;
  readonly frames: Frame[];
  speed: number;
  playing = true;
  loop = true;
  private time: number;
  private index = -1;
  private timer: number | null = null;

  constructor(frames: Frame[], speed = 10) {
    super();
    if (frames.length === 0) throw new Error("a replay needs at least one frame");
    this.frames = frames;
    this.speed = speed;
    this.time = frames[0].t;
    this.seek(this.time);
  }

  get start_t(): number {
    return this.frames[0].t;
  }

  get end_t(): number {
    return this.frames[this.frames.length - 1].t;
  }

  get t(): number {
    return this.time;
  }

  /** The last frame at or before simulation time `t`. */
  seek(t: number): void {
    this.time = Math.min(Math.max(t, this.start_t), this.end_t);
    let lo = 0;
    let hi = this.frames.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.frames[mid].t <= this.time) lo = mid;
      else hi = mid - 1;
    }
    if (lo !== this.index) {
      this.index = lo;
      this.emit(this.frames[lo]);
    }
  }

  advance(wallSeconds: number): void {
    if (!this.playing) return;
    let next = this.time + wallSeconds * this.speed;
    if (next > this.end_t) {
      if (!this.loop) {
        this.playing = false;
        next = this.end_t;
      } else {
        next = this.start_t;
        this.index = -1;
      }
    }
    this.seek(next);
  }

  startClock(): void {
    if (this.timer !== null || typeof window === "undefined") return;
    let last = performance.now();
    const tick = () => {
      const now = performance.now();
      this.advance(Math.min((now - last) / 1000, 0.25));
      last = now;
      this.timer = window.requestAnimationFrame(tick);
    };
    this.timer = window.requestAnimationFrame(tick);
  }

  close(): void {
    if (this.timer !== null) window.cancelAnimationFrame(this.timer);
    this.timer = null;
  }
}

/** The frame stream of a live runtime session (`/api/runtime/sessions/{id}/stream`). */
export class LiveSource extends Emitter implements FrameSource {
  readonly kind = "live" as const;
  private socket: WebSocket | null = null;
  private closed = false;
  private retry = 500;

  constructor(
    readonly url: string,
    private readonly makeSocket: (url: string) => WebSocket = (u) => new WebSocket(u),
  ) {
    super();
    this.connect();
  }

  private connect(): void {
    if (this.closed) return;
    const socket = this.makeSocket(this.url);
    this.socket = socket;
    socket.onmessage = (event: MessageEvent<string>) => {
      this.retry = 500;
      this.emit(JSON.parse(event.data) as Frame);
    };
    socket.onclose = (event: CloseEvent) => {
      if (this.closed || event.code === 4404) return;
      window.setTimeout(() => this.connect(), this.retry);
      this.retry = Math.min(this.retry * 2, 8000);
    };
  }

  close(): void {
    this.closed = true;
    this.socket?.close();
  }
}

export function streamUrl(api: string, session: string): string {
  const base = new URL(api, window.location.href);
  base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
  base.pathname = `${base.pathname.replace(/\/$/, "")}/runtime/sessions/${encodeURIComponent(session)}/stream`;
  return base.toString();
}
