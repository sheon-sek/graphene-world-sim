import { useEffect, useRef } from "react";
import { create } from "zustand";
import { api, apiBase, ApiError, must, type SessionInfo } from "../api/client";
import { LiveSource, streamUrl } from "../live/frames";
import { useSim } from "../live/store";

/**
 * The runtime session the workspaces are open on: its description, its frame stream (through
 * `useSim`), and the lifecycle calls. A call that fails leaves its message in `error`.
 */
export interface SessionState {
  sid: string | null;
  info: SessionInfo | null;
  /** What a long call is doing ("Compiling the models…"), while it runs. */
  busy: string | null;
  error: string | null;
  /** Bumped after every call that changes the trajectory, so panels refetch. */
  version: number;
  open(sid: string): Promise<void>;
  refresh(): Promise<void>;
  act<T>(label: string | null, call: () => Promise<T>): Promise<T | undefined>;
  dismiss(): void;
}

const path = (sid: string) => ({ params: { path: { sid } } });

export const useSession = create<SessionState>((set, get) => ({
  sid: null,
  info: null,
  busy: null,
  error: null,
  version: 0,
  async open(sid) {
    if (get().sid === sid) return;
    set({ sid, info: null, error: null, version: 0 });
    useSim.getState().connect(new LiveSource(streamUrl(apiBase(), sid)));
    await get().refresh();
  },
  async refresh() {
    const sid = get().sid;
    if (!sid) return;
    try {
      const info = await must(api.GET("/api/runtime/sessions/{sid}", path(sid)));
      if (get().sid === sid) set({ info });
    } catch (e) {
      set({ error: String(e instanceof ApiError ? e.message : e) });
    }
  },
  async act(label, call) {
    set({ busy: label, error: null });
    try {
      const result = await call();
      set((s) => ({ version: s.version + 1 }));
      await get().refresh();
      return result;
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
      return undefined;
    } finally {
      set({ busy: null });
    }
  },
  dismiss() {
    set({ error: null });
  },
}));

export { path as sessionPath };

/** Lifecycle calls on the open session. */
export const lifecycle = {
  run: (sid: string, speed?: number) =>
    useSession.getState().act(null, () => must(api.POST("/api/runtime/sessions/{sid}/run", { ...path(sid), body: { speed } }))),
  pause: (sid: string) => useSession.getState().act(null, () => must(api.POST("/api/runtime/sessions/{sid}/pause", path(sid)))),
  step: (sid: string, steps: number) =>
    useSession.getState().act(null, () => must(api.POST("/api/runtime/sessions/{sid}/step", { ...path(sid), body: { steps } }))),
  speed: (sid: string, speed: number) =>
    useSession.getState().act(null, () => must(api.PUT("/api/runtime/sessions/{sid}/speed", { ...path(sid), body: { speed } }))),
  reinit: (sid: string, revision: number, scope: string[]) =>
    useSession
      .getState()
      .act("Rebuilding the models. The first build of a changed scope compiles it and can take several minutes.", () =>
        must(api.POST("/api/runtime/sessions/{sid}/reinit", { ...path(sid), body: { revision, scope } })),
      ),
};

/** Call `fn` now and every `ms` milliseconds, and again whenever `deps` change. */
export function usePoll(fn: () => void | Promise<void>, ms: number, deps: unknown[]): void {
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => {
    let alive = true;
    const tick = () => {
      if (alive && document.visibilityState !== "hidden") void latest.current();
    };
    tick();
    const id = window.setInterval(tick, ms);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, ...deps]);
}
