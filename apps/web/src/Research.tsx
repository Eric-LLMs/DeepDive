// Research OS console (web): read-only two-layer monitor mirroring the desktop sidebar.
//
// Tasks are created and driven entirely in the desktop client chat — "＋ Research" POSTs
// /research/tasks atomically, then the deep_research skill drives the stage machine. This
// console never writes: no create form, no Promote button; stage transitions, gate overrides,
// scratch writes and Promote all happen agent-side in the chat. It just reflects whatever the
// agent produced, with the selected task's status in the lower-left layer and the opened
// report rendered inline on the right.
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { renderMarkdown } from "./markdown";
import type {
  ResearchArtifact,
  ResearchTask,
  ResearchTaskDetail,
} from "./types";

const STAGES = ["DISCOVER", "FRAME", "EVIDENCE", "EXECUTE", "WRITE", "PUBLISH"];
const GATE_LABELS: Record<string, string> = {
  EVIDENCE_GATE: "Evidence Gate",
  CLAIM_GATE: "Claim Gate",
};

const card: React.CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: 10,
  marginBottom: 8,
  background: "var(--bg)",
  cursor: "pointer",
};
const btn: React.CSSProperties = {
  padding: "4px 10px",
  fontSize: 12,
  borderRadius: 6,
  border: "1px solid var(--border)",
  background: "var(--bg)",
  color: "var(--fg)",
  cursor: "pointer",
};
const sectionLabel: React.CSSProperties = {
  fontSize: 11,
  color: "var(--fg-dim)",
  textTransform: "uppercase",
  letterSpacing: 0.4,
  marginBottom: 4,
};

export default function Research() {
  const [tasks, setTasks] = useState<ResearchTask[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ResearchTaskDetail | null>(null);
  const [artifact, setArtifact] = useState<{ title: string; html: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await api.research.listTasks();
      setTasks(res.tasks);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const openTask = async (taskId: string) => {
    setSelectedId(taskId);
    setArtifact(null);
    try {
      setDetail(await api.research.getTask(taskId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const viewArtifact = async (a: ResearchArtifact) => {
    try {
      const res = await api.research.getArtifact(a.task_id, a.artifact_id, a.version);
      setArtifact({ title: `${a.artifact_id} v${a.version}`, html: renderMarkdown(res.content) });
      setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div style={{ display: "flex", gap: 16, height: "100%" }}>
      {/* Left column: task list (top) + selected task status (bottom), both read-only. */}
      <div style={{ width: 320, flexShrink: 0, display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <span style={{ flex: 1, fontSize: 12, color: "var(--fg-dim)", alignSelf: "center" }}>
            Tasks are created in the desktop chat (＋ Research).
          </span>
          <button style={btn} onClick={refresh}>↻</button>
        </div>
        {error && <div style={{ color: "#f87171", fontSize: 12, marginBottom: 8 }}>{error}</div>}

        <div style={sectionLabel}>Tasks</div>
        <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
          {tasks.length === 0 && (
            <div style={{ color: "var(--fg-dim)", fontSize: 12, padding: 8 }}>
              No tasks yet — create one with ＋ Research in the desktop chat.
            </div>
          )}
          {tasks.map((t) => (
            <div
              key={t.task_id}
              style={{ ...card, outline: selectedId === t.task_id ? "1px solid var(--accent)" : undefined }}
              onClick={() => openTask(t.task_id)}
            >
              <div style={{ fontSize: 13 }}>{t.name || t.task_id}</div>
              <div style={{ fontSize: 11, color: "var(--fg-dim)" }}>Stage {t.stage} · {t.status}</div>
            </div>
          ))}
        </div>

        <div style={{ ...sectionLabel, marginTop: 16 }}>Status</div>
        <div style={{ flex: 1, minHeight: 0, overflow: "auto", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          {detail ? (
            <>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4, wordBreak: "break-word" }}>
                {detail.name || detail.task_id}
              </div>
              <div style={{ fontSize: 11, color: "var(--fg-dim)", marginBottom: 8, lineHeight: 1.4 }}>
                {detail.status}{detail.description ? ` — ${detail.description}` : ""}
              </div>

              <div style={sectionLabel}>Stage</div>
              <div style={{ display: "flex", gap: 3, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
                {STAGES.map((s, i) => {
                  const idx = STAGES.indexOf(detail.stage);
                  const state = s === detail.stage ? "current" : i < idx ? "done" : "pending";
                  const pill: React.CSSProperties = {
                    display: "inline-flex", alignItems: "center", gap: 4,
                    padding: "3px 8px", fontSize: 11, borderRadius: 12,
                    border: "1px solid var(--border)", color: "var(--fg-dim)",
                  };
                  if (state === "done") { pill.color = "var(--fg)"; pill.borderColor = "var(--accent)"; }
                  if (state === "current") { pill.background = "var(--accent)"; pill.borderColor = "var(--accent)"; pill.color = "#fff"; pill.fontWeight = 600; }
                  const mark = state === "done" ? "✓" : state === "current" ? "●" : "○";
                  return (
                    <span key={s}>
                      {i > 0 && <span style={{ fontSize: 11, color: "var(--fg-dim)", marginRight: 3 }}>➔</span>}
                      <span style={pill}><span style={{ fontSize: 10 }}>{mark}</span> {s}</span>
                    </span>
                  );
                })}
              </div>

              <div style={sectionLabel}>Gates</div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 10 }}>
                {Object.entries(detail.gates)
                  .filter(([name]) => GATE_LABELS[name])
                  .map(([name, status]) => (
                    <span key={name} style={{
                      fontSize: 11, padding: "2px 8px", borderRadius: 10,
                      border: "1px solid var(--border)", color: status === "PASS" || status === "OVERRIDE" ? "#34d399" : "var(--fg-dim)",
                    }}>
                      {GATE_LABELS[name]}: {status}
                    </span>
                  ))}
              </div>

              <div style={sectionLabel}>Evidence graph</div>
              <div style={{ marginBottom: 10 }}>
                {(["Source", "Claim", "Evidence"] as const).map((type) => {
                  const nodes = detail.nodes?.[type] || [];
                  if (!nodes.length) return null;
                  return (
                    <div key={type} style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center", marginBottom: 4 }}>
                      <span style={{ width: 60, fontSize: 11, color: "var(--fg-dim)" }}>{type}</span>
                      {nodes.map((n) => (
                        <span key={n.id} title={`${n.id} · ${n.status || "VALID"}`} style={{
                          fontSize: 11, padding: "2px 8px", borderRadius: 10, border: "1px solid var(--border)",
                          color: "var(--fg)", background: "var(--bg)",
                        }}>{n.label || n.id}</span>
                      ))}
                    </div>
                  );
                })}
                {!detail.nodes?.Source?.length && !detail.nodes?.Claim?.length && !detail.nodes?.Evidence?.length && (
                  <div style={{ fontSize: 12, color: "var(--fg-dim)" }}>No evidence recorded yet.</div>
                )}
              </div>

              <div style={sectionLabel}>Materials &amp; outputs</div>
              <div style={{ display: "flex", gap: 12, marginBottom: 4 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 11, color: "var(--fg-dim)", marginBottom: 2 }}>Materials ({detail.materials.length})</div>
                  {detail.materials.length === 0 && <div style={{ fontSize: 11, color: "var(--fg-dim)" }}>None</div>}
                  {detail.materials.slice(0, 12).map((m) => (
                    <div key={m} style={{ fontSize: 11, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{m}</div>
                  ))}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 11, color: "var(--fg-dim)", marginBottom: 2 }}>Outputs ({detail.outputs.length})</div>
                  {detail.outputs.length === 0 && <div style={{ fontSize: 11, color: "var(--fg-dim)" }}>None yet</div>}
                  {detail.outputs.slice(0, 12).map((o) => (
                    <div key={o} style={{ fontSize: 11, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{o}</div>
                  ))}
                </div>
              </div>
            </>
          ) : (
            <div style={{ color: "var(--fg-dim)", fontSize: 12, padding: 8 }}>Select a task to see its progress.</div>
          )}
        </div>
      </div>

      {/* Right pane: opened report (read-only) or guide hint. */}
      <div style={{ flex: 1, minWidth: 0, overflow: "auto" }}>
        {artifact ? (
          <div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
              <button style={btn} onClick={() => setArtifact(null)}>← Task</button>
              <strong>{artifact.title}</strong>
            </div>
            <div className="markdown-body" style={{ fontSize: 13, lineHeight: 1.6 }} dangerouslySetInnerHTML={{ __html: artifact.html }} />
          </div>
        ) : (
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "center", padding: 32 }}>
            <div style={{ width: "100%", maxWidth: 560, border: "1px solid var(--border)", borderRadius: 12, padding: 28, textAlign: "center", background: "var(--bg)" }}>
              <h2 style={{ margin: "0 0 8px", fontSize: 18, fontWeight: 600, color: "var(--fg)" }}>
                Investigate questions with your AI agent
              </h2>
              <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--fg-dim)", lineHeight: 1.5 }}>
                Research tasks are created in the <strong>desktop client chat</strong> — click
                <strong> ＋ Research</strong> in the chat header, then the agent drives the task
                through <strong>DISCOVER → … → PUBLISH</strong> while you watch progress here.
              </p>
              {detail && detail.artifacts.length > 0 && (
                <div style={{ marginTop: 16, textAlign: "left", borderTop: "1px solid var(--border)", paddingTop: 12 }}>
                  <div style={sectionLabel}>Artifacts</div>
                  {detail.artifacts.map((a) => (
                    <div key={`${a.artifact_id}@${a.version}`} style={{ ...card, cursor: "default" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div style={{ flex: 1 }}>
                          <div style={{ fontSize: 13 }}>{a.artifact_id} · v{a.version}</div>
                          <div style={{ fontSize: 11, color: "var(--fg-dim)" }}>
                            Status {a.status}{a.drive_path ? ` · ${a.drive_path}` : ""}
                          </div>
                        </div>
                        <button style={btn} onClick={() => viewArtifact(a)}>View</button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
