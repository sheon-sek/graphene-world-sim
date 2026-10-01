import { create } from "zustand";
import type { Frame, FrameSource } from "./frames";

/**
 * Client state shared by the 3D view and the panels. The 3D view reads `frame` inside its
 * render loop through `useSim.getState()`, so a new frame never re-renders the scene graph;
 * panels subscribe with selectors and re-render only when their slice changes.
 */
export interface SimState {
  source: FrameSource | null;
  frame: Frame | null;
  /** Wall-clock time the current frame arrived, for staleness. */
  receivedAt: number;
  selected: string | null;
  hovered: string | null;
  connect(source: FrameSource): void;
  select(id: string | null): void;
  hover(id: string | null): void;
}

let unsubscribe: (() => void) | null = null;

export const useSim = create<SimState>((set, get) => ({
  source: null,
  frame: null,
  receivedAt: 0,
  selected: null,
  hovered: null,
  connect(source) {
    unsubscribe?.();
    get().source?.close();
    set({ source, frame: null });
    unsubscribe = source.subscribe((frame) => set({ frame, receivedAt: performance.now() }));
  },
  select(selected) {
    set({ selected });
  },
  hover(hovered) {
    if (get().hovered !== hovered) set({ hovered });
  },
}));

declare global {
  interface Window {
    /** The client store, for end-to-end tests and the browser console. */
    gws?: { store: typeof useSim; camera?: unknown };
  }
}

if (typeof window !== "undefined") window.gws = { store: useSim };
