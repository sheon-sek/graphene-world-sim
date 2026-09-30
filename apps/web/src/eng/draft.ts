import { create } from "zustand";
import {
  api,
  must,
  type Asset,
  type Connection,
  type Diff,
  type Draft,
  type Edit,
  type Operation,
  type Revision,
  type Validation,
} from "../api/client";
import type { WorldData } from "../live/world";

/**
 * The Engineering workspace's draft: edits staged against the head revision, their
 * validation and their diff (with the change class of each change), until they are applied
 * as a new revision or discarded (#54). Edits live on the server, so a reload keeps them.
 */
export interface DraftState {
  head: number | null;
  draft: Draft | null;
  validation: Validation | null;
  diff: Diff | null;
  /** Assets added in this draft that the session should simulate once it is applied. */
  simulate: string[];
  applied: Revision | null;
  error: string | null;
  load(): Promise<void>;
  stage(edits: Edit[]): Promise<boolean>;
  discard(): Promise<void>;
  apply(message: string): Promise<Revision | null>;
  setSimulate(id: string, on: boolean): void;
  forgetApplied(): void;
}

const AUTHOR = "web";

async function checks(id: string) {
  const path = { params: { path: { draft_id: id } } };
  return Promise.all([
    must(api.GET("/api/world-model/drafts/{draft_id}/validation", path)),
    must(api.GET("/api/world-model/drafts/{draft_id}/diff", path)),
  ]);
}

export const useDraft = create<DraftState>((set, get) => ({
  head: null,
  draft: null,
  validation: null,
  diff: null,
  simulate: [],
  applied: null,
  error: null,
  async load() {
    try {
      const [summary, drafts] = await Promise.all([
        must(api.GET("/api/world-model/summary")),
        must(api.GET("/api/world-model/drafts")),
      ]);
      const mine = drafts
        .filter((d) => d.author === AUTHOR && d.base === summary.revision)
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
      set({ head: summary.revision, draft: mine ?? null, error: null });
      if (mine) {
        const [validation, diff] = await checks(mine.id);
        const added = diff.changes.filter((c) => c.collection === "assets" && c.kind === "added").map((c) => c.key);
        set({ validation, diff, simulate: added });
      } else set({ validation: null, diff: null });
    } catch (e) {
      set({ error: String(e) });
    }
  },
  async stage(edits) {
    try {
      let draft = get().draft;
      if (!draft) draft = await must(api.POST("/api/world-model/drafts", { body: { author: AUTHOR } }));
      draft = await must(
        api.POST("/api/world-model/drafts/{draft_id}/operations", {
          params: { path: { draft_id: draft.id } },
          body: edits as Operation[],
        }),
      );
      const [validation, diff] = await checks(draft.id);
      set({ draft, validation, diff, error: null, applied: null });
      return true;
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
      return false;
    }
  },
  async discard() {
    const draft = get().draft;
    if (draft) await must(api.DELETE("/api/world-model/drafts/{draft_id}", { params: { path: { draft_id: draft.id } } }));
    set({ draft: null, validation: null, diff: null, simulate: [], error: null });
  },
  async apply(message) {
    const draft = get().draft;
    if (!draft) return null;
    try {
      const revision = await must(
        api.POST("/api/world-model/drafts/{draft_id}/apply", {
          params: { path: { draft_id: draft.id } },
          body: { message, author: AUTHOR },
        }),
      );
      set({ draft: null, validation: null, diff: null, applied: revision, head: revision.number, error: null });
      return revision;
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) });
      return null;
    }
  },
  setSimulate(id, on) {
    set((s) => ({ simulate: on ? [...new Set([...s.simulate, id])] : s.simulate.filter((x) => x !== id) }));
  },
  forgetApplied() {
    set({ applied: null, simulate: [] });
  },
}));

export interface Edited {
  assets: Map<string, Asset>;
  connections: Map<string, Connection>;
  /** Keys a draft operation touched, to mark them in the views. */
  touched: Set<string>;
}

/** The revision with the draft's asset and connection operations applied, as the server does. */
export function overlay(world: WorldData, operations: readonly Operation[]): Edited {
  const assets = new Map(world.assets);
  const connections = new Map(world.connections);
  const touched = new Set<string>();
  for (const op of operations) {
    if (op.op === "put" && op.collection === "assets") {
      const value = op.value as unknown as Asset;
      assets.set(value.id, value);
      touched.add(value.id);
    } else if (op.op === "put" && op.collection === "connections") {
      const value = op.value as unknown as Connection;
      connections.set(value.id, value);
      touched.add(value.id);
    } else if (op.op === "delete" && op.collection === "assets") {
      assets.delete(op.key);
      touched.add(op.key);
    } else if (op.op === "delete" && op.collection === "connections") {
      connections.delete(op.key);
      touched.add(op.key);
    }
  }
  return { assets, connections, touched };
}

/** A connection id in the importer's convention: `chw:~CB-001->FCU/L1_FCU1`, `air:X->DH01`. */
export function connectionId(domain: string, source: string, target: string): string {
  const end = (node: string) => node.replace(/^room:/, "");
  return `${domain}:${end(source)}->${end(target)}`;
}
