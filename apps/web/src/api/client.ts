/**
 * The typed API client. `schema.d.ts` is generated from `docs/api/openapi.json`
 * (`pnpm api:gen`), and `pnpm api:check` fails when it is out of date, so a change to the API
 * that the web app does not follow fails the build.
 */
import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type SessionInfo = Schemas["SessionOut"];
export type Preset = Schemas["Preset"];
export type ComponentType = Schemas["ComponentType"];
export type Asset = Schemas["Asset"];
export type Connection = Schemas["Connection"];
export type Site = Schemas["Site-Output"];
export type Draft = Schemas["DraftOut"];
export type Diff = Schemas["DiffOut"];
export type Validation = Schemas["ValidationOut"];
export type Revision = Schemas["RevisionOut"];
export type Operation = Draft["operations"][number];

/** Where the API lives: `?api=` when given, otherwise `api/` beside the page. */
export function apiBase(): string {
  const given = new URLSearchParams(window.location.search).get("api");
  const base = given ?? new URL("api", document.baseURI).href;
  return base.replace(/\/$/, "");
}

/** The server origin the generated paths (which start with `/api`) are relative to. */
function serverRoot(): string {
  return apiBase().replace(/\/api$/, "");
}

// `fetch` is looked up per call, so tests can stub it after this module loads.
export const api = createClient<paths>({ baseUrl: serverRoot(), fetch: (request) => globalThis.fetch(request) });

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(ApiError.describe(status, detail));
  }

  static describe(status: number, detail: unknown): string {
    const d = (detail as { detail?: unknown } | null)?.detail ?? detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d))
      return d
        .map((x: { msg?: string; message?: string; path?: string; loc?: unknown[] }) =>
          [x.path ?? x.loc?.join("."), x.msg ?? x.message].filter(Boolean).join(": "),
        )
        .join("; ");
    return `HTTP ${status}`;
  }
}

/** The data of an openapi-fetch result, or an ApiError carrying the server's `detail`. */
export async function must<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok) throw new ApiError(response.status, error);
  return data as T;
}
export type Put = Schemas["Put"];
export type Delete = Schemas["Delete"];
export type Edit = Put | Delete;
