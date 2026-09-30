import { useState } from "react";
import { shortName } from "../app/format";
import { lifecycle, useSession } from "../app/session";
import { useDraft } from "./draft";

const CLASS_NOTE: Record<string, string> = {
  live: "applied at the next step",
  warm: "partition rebuilt, state carried over",
  structural: "partition recompiled",
  reinitialise: "whole simulation rebuilt",
};

/** The staged edits: whether they validate, what they change and how, and applying them. */
export function DraftPanel({ sid }: { sid: string }) {
  const { draft, validation, diff, applied, error, head, simulate, apply, discard, forgetApplied } = useDraft();
  const info = useSession((s) => s.info);
  const [message, setMessage] = useState("");
  const issues = validation?.issues ?? [];

  async function reinit(revision: number) {
    if (!info) return;
    const scope = [...new Set([...info.scope, ...simulate])];
    const frame = await lifecycle.reinit(sid, revision, scope);
    if (frame) forgetApplied();
  }

  const behind = info?.revision != null && head != null && info.revision < head;
  return (
    <section className="panel draft-panel" data-testid="draft-panel">
      <header className="panel-head">
        <h3>Draft</h3>
        {diff?.overall && <span className={`tag cc-${diff.overall}`}>{diff.overall}</span>}
      </header>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {!draft && !applied && (
        <p className="muted">
          No staged changes. Head revision {head ?? "–"}; this session runs revision {info?.revision ?? "–"}.
        </p>
      )}
      {!draft && (applied || behind) && (
        <div className="applied" data-testid="applied">
          {applied && (
            <p>
              Revision {applied.number} applied: {applied.message}
            </p>
          )}
          <button className="primary" onClick={() => void reinit(applied?.number ?? head ?? 0)} data-testid="reinit">
            Reinitialise the session on revision {applied?.number ?? head}
          </button>
          {simulate.length > 0 && <p className="muted">Adds to the scope: {simulate.map(shortName).join(", ")}.</p>}
        </div>
      )}
      {draft && (
        <>
          <p className="muted">
            {draft.operations.length} operation{draft.operations.length === 1 ? "" : "s"} on revision {draft.base}.
          </p>
          <div className={`validation ${validation?.valid ? "ok" : "bad"}`} data-testid="validation">
            {validation?.valid ? "Valid" : "Does not validate"}
            {issues.length > 0 && ` · ${issues.length} issue${issues.length === 1 ? "" : "s"}`}
          </div>
          {issues.length > 0 && (
            <ul className="log issues">
              {issues.map((i, n) => (
                <li key={n} className={i.severity}>
                  <b>{i.severity}</b> <code>{i.path}</code> {i.message}
                </li>
              ))}
            </ul>
          )}
          <table className="table diff" data-testid="diff">
            <tbody>
              {diff?.changes.map((c) => (
                <tr key={`${c.collection}:${c.key}`}>
                  <td>{c.kind}</td>
                  <td title={c.key}>{c.collection === "connections" ? c.key : shortName(c.key)}</td>
                  <td>
                    <span className={`tag cc-${c.change_class}`} title={CLASS_NOTE[c.change_class]}>
                      {c.change_class}
                    </span>
                  </td>
                  <td className="muted">{c.fields.length ? c.fields.join(", ") : c.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="inline-form">
            <input placeholder="What changed" value={message} onChange={(e) => setMessage(e.target.value)} data-testid="apply-message" />
            <button
              className="primary"
              disabled={!validation?.valid}
              onClick={() => void apply(message.trim() || "Edit from the Engineering workspace").then(() => setMessage(""))}
              data-testid="apply-draft"
            >
              Apply as new revision
            </button>
            <button className="danger" onClick={() => void discard()}>
              Discard
            </button>
          </div>
        </>
      )}
    </section>
  );
}
