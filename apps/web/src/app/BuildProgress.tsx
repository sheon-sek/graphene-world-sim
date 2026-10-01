import { useEffect, useState } from "react";
import { api, must, type Schemas } from "../api/client";

type Build = Schemas["BuildOut"];

const PHASE: Record<string, string> = {
  waiting: "Waiting for the same model's build to finish",
  downloading: "Downloading the prebuilt model",
  translating: "Translating the Modelica model (the longest, most memory-hungry step)",
  generating: "Writing C code",
  compiling: "Compiling C code",
  packaging: "Packaging the model",
};

export function elapsed(s: number): string {
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

/** One line per model being fetched or built: its step, how far it is and the memory used. */
export function describe(b: Build): { title: string; detail: string; fraction: number | null } {
  const fraction = b.phase === "compiling" && b.total > 0 ? b.done / b.total : null;
  const files = b.phase === "compiling" ? ` ${b.done} of ${b.total} files` : b.phase === "generating" ? ` (${b.done} files)` : "";
  const memory =
    b.memory_gb != null
      ? `memory ${b.memory_gb.toFixed(1)} GB${b.memory_limit_gb != null ? ` of ${b.memory_limit_gb} GB` : ""}, needs about ${b.memory_needed_gb} GB`
      : `needs about ${b.memory_needed_gb} GB of memory`;
  return {
    title: `${PHASE[b.phase] ?? b.phase}${files}`,
    detail: `${b.assets} assets · ${elapsed(b.elapsed_s)} elapsed · ${memory}`,
    fraction,
  };
}

/** Models being built right now, polled while there are any (or while `watch` is set). */
export function BuildProgress({ watch = false }: { watch?: boolean }) {
  const [builds, setBuilds] = useState<Build[]>([]);
  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    const poll = async () => {
      const found = await must(api.GET("/api/runtime/builds")).catch(() => [] as Build[]);
      if (stopped) return;
      setBuilds(found);
      timer = window.setTimeout(poll, found.length || watch ? 2000 : 10000);
    };
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [watch]);
  if (!builds.length) return null;
  return (
    <div className="builds" role="status" data-testid="builds">
      {builds.map((b) => {
        const d = describe(b);
        return (
          <div key={b.partition} className="build">
            <span className="spinner" />
            <div>
              <b>{d.title}</b>
              <progress max={1} value={d.fraction ?? undefined} />
              <small className="muted">{d.detail}</small>
            </div>
          </div>
        );
      })}
    </div>
  );
}
