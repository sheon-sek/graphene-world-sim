import { frame } from "../test/fixtures";
import { LiveSource, ReplaySource, stateNumber, type Frame } from "./frames";

const frames = [0, 5, 10, 15, 20].map((t) => frame(t, { room: { TAir: 290 + t } }));

test("a replay emits the frame at the playhead as simulation time advances", () => {
  const replay = new ReplaySource(frames, 10);
  const seen: number[] = [];
  replay.subscribe((f) => seen.push(f.t));
  expect(seen).toEqual([0]);
  replay.advance(0.4); // 4 s of simulation time: still the first frame
  replay.advance(0.2); // 6 s
  replay.advance(1.0); // 16 s
  expect(seen).toEqual([0, 5, 15]);
});

test("a replay seeks to the last frame at or before a time, and loops at the end", () => {
  const replay = new ReplaySource(frames, 10);
  let last: Frame | null = null;
  replay.subscribe((f) => (last = f));
  replay.seek(12.5);
  expect(last!.t).toBe(10);
  replay.seek(20);
  replay.advance(1);
  expect(replay.t).toBe(0);
  replay.loop = false;
  replay.seek(18);
  replay.advance(1);
  expect(replay.playing).toBe(false);
  expect(replay.t).toBe(20);
});

test("a paused replay does not move", () => {
  const replay = new ReplaySource(frames, 10);
  replay.playing = false;
  replay.advance(5);
  expect(replay.t).toBe(0);
});

test("a live source parses frames from its socket", () => {
  const sockets: Record<string, ((e: unknown) => void) | null>[] = [];
  const live = new LiveSource("ws://x/stream", () => {
    const s = { onmessage: null, onclose: null, close() {} };
    sockets.push(s);
    return s as unknown as WebSocket;
  });
  const seen: number[] = [];
  live.subscribe((f) => seen.push(f.t));
  sockets[0].onmessage!({ data: JSON.stringify(frame(35)) });
  expect(seen).toEqual([35]);
  live.close();
});

test("state numbers fall back when a signal is missing or not numeric", () => {
  const f = frame(0, { a: { x: 3, on: true, s: "text" } });
  expect(stateNumber(f, "a", "x")).toBe(3);
  expect(stateNumber(f, "a", "on")).toBe(1);
  expect(stateNumber(f, "a", "s", -1)).toBe(-1);
  expect(stateNumber(f, "missing", "x", 7)).toBe(7);
  expect(stateNumber(null, "a", "x", 2)).toBe(2);
});
