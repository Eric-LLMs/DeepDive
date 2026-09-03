/* Research OS monitor (desktop).
 *
 * Two-layer console: the sidebar lists tasks (top) and shows the selected task's live status
 * (bottom). Tasks are created in the chat — "＋ Research" POSTs /research/tasks atomically,
 * then the deep_research skill drives the stage machine. The panel starts/resumes a run with
 * its Run control (which sends the run message through the task's bound session); every other
 * stage transition, gate override, scratch write and Promote happens agent-side in the chat,
 * so the monitor reflects whatever the agent produced. While a run is in flight, Run + Delete
 * Task are disabled and a live Activity feed below the working directory mirrors the events.
 */
(() => {
  "use strict";
  const { apiFetch } = window;
  const toast = (msg) => window.Viewer && window.Viewer.toast(msg);

  const tasksList = document.getElementById("research-tasks-list");
  const statusBody = document.getElementById("research-status-body");

  // Every chat session bound to one of this user's research tasks as
  // session_id → { task_id, name, stage, status }. These are a different kind than normal
  // chats: hidden from the Sessions sidebar, one per task (1:1, bound at creation), and
  // opened silently when the task is selected. Stage/status are refreshed on every status
  // load so the 🔬 chat badge (app.js updateResearchChip) stays live.
  window.researchSessions = window.researchSessions || new Map();

  function recordResearchSession(detail) {
    if (detail && detail.session_id) {
      window.researchSessions.set(detail.session_id, {
        task_id: detail.task_id,
        name: detail.name || detail.task_id,
        stage: detail.stage || null,
        status: detail.status || null,
      });
      if (window.updateResearchChip) window.updateResearchChip();
    }
  }

  // Discrete stage ladder (the literature profile skips DESIGN/EXPLAIN/REPRODUCE).
  const STAGES = ["DISCOVER", "FRAME", "EVIDENCE", "EXECUTE", "WRITE", "PUBLISH"];
  const GATE_NAMES = { EVIDENCE_GATE: "Evidence Gate", CLAIM_GATE: "Claim Gate" };

  // Activity feed shows normalized action summaries only — never the agent's raw reasoning
  // (thinking fragments like "_handoff" / "Let me try…"). Tool-call names map to a short,
  // human-readable action; unknown names fall back to a cleaned-up tool name.
  const ACTIVITY_TOOL_LABELS = {
    web_search: "Searching the web",
    rag_search: "Querying the knowledge base",
    research_evidence: "Recording evidence",
    research_artifact: "Writing artifact",
    research_gate: "Checking gates",
    research_run: "Run control",
    research_project: "Updating project",
    research_state: "Reading research state",
  };
  function activityToolLabel(name) {
    const n = String(name || "").toLowerCase();
    if (ACTIVITY_TOOL_LABELS[n]) return ACTIVITY_TOOL_LABELS[n];
    if (n.includes("search")) return "Searching the web";
    if (n.includes("artifact")) return "Writing artifact";
    if (n.includes("evidence")) return "Recording evidence";
    if (n.includes("gate")) return "Checking gates";
    if (n.includes("write") || n === "text" || n.includes("append") || n.includes("file")) {
      return "Writing a file";
    }
    if (n.includes("read") || n.includes("grep") || n.includes("glob") || n.includes("list")) {
      return "Reading files";
    }
    if (n.includes("python") || n.includes("bash") || n.includes("shell")) return "Running a command";
    return name ? String(name).replace(/_/g, " ") : "Tool";
  }

  let selectedTask = null; // { task_id, name }
  // Research run state: a run is in flight when the desktop auto-started it (Run) or the user
  // activated it by typing in the task's session (sendChat attaches the research handoff).
  // While running, the Run + Delete Task controls stay disabled and the Run button shows its
  // running style; the Activity feed below mirrors the live run events.
  let researchRunning = false;
  let runningTaskId = null;      // task the in-flight run belongs to (blocks deleting it)
  let activityEl = null;         // live Activity section (survives same-task pane re-renders)
  let activityForTask = null;    // task_id the current activity feed belongs to
  // Live revision monitor (one SSE stream per selected task): the server sends a snapshot
  // then only revision-increments, so the desktop refetches the authoritative status exactly
  // when something changed — never on a blind timer. Stale/duplicate hints are dropped.
  let monitorTaskId = null;
  let monitorAbort = null;       // AbortController for the open monitor stream
  let monitorRevision = 0;       // last applied project_revision (stale events ignored)
  let refreshQueued = false;     // throttle: coalesce change bursts into ≤2s refetches
  let refreshTimer = null;
  // Working-directory tree expansion, keyed per task, survives live re-renders.
  const treeOpenState = new Map(); // task_id -> Set(relative folder seg)
  let stopInFlight = false;
  // Two-zone workbench: the right-hand file-preview column is persistent per task (it survives
  // monitor re-renders so an open file's content is not wiped every refresh), while the tree
  // column rebuilds in place keeping its expansion set above. previewState records the file
  // currently shown so a live refresh can detect the agent rewrote it (size change).
  let previewColEl = null;        // persistent right-column preview panel for the open task
  let previewTitleEl = null;      // its path label
  let previewBodyEl = null;       // its scrollable content area
  let previewState = { taskId: null, file: null, size: null };
  let previewRefreshedAt = 0;     // throttle: don't refetch an open file more than ~1/s

  function bearerToken() {
    try { return localStorage.getItem("deepdive_token"); } catch { return null; }
  }

  function escapeHtml(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function renderMarkdown(text) {
    const src = String(text ?? "");
    const md = window.markdownit && window.markdownit({ html: false, linkify: true, breaks: true });
    return md ? md.render(src) : `<pre>${escapeHtml(src)}</pre>`;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  // Compact "last activity" label for a task row (from the task's updated_at).
  function timeAgo(iso) {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) return "just now";
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.floor(h / 24);
    if (d < 30) return `${d}d ago`;
    return iso.slice(0, 10);
  }

  function section(title) {
    const box = el("div", "research-section");
    box.appendChild(el("div", "research-section-title", title));
    return box;
  }

  // ── run controls + live Activity feed ─────────────────────────────────────
  // The run state is shared with app.js: sendChat announces start/end via researchRunActive and
  // mirrors each streamed event via researchActivityEvent; the 10s status poll re-asserts a run
  // that is already RUNNING server-side. Starting a run always funnels through
  // window.startResearchRun (app.js), which opens the session then auto-sends the run message.
  function runStartText() { return researchRunning ? "● Running…" : "▶ Run"; }

  function syncRunCtl() {
    document.querySelectorAll(".run-ctl").forEach((btn) => {
      if (btn.classList.contains("run-stop")) {
        // Stop is the mirror of Run: only actionable while a run is in flight.
        btn.disabled = !researchRunning;
      } else if (btn.classList.contains("run-start")) {
        btn.disabled = researchRunning;
        btn.textContent = runStartText();
        btn.classList.toggle("running", researchRunning);
      } else {
        btn.disabled = researchRunning; // Delete Task / other run-scoped controls
      }
    });
  }

  // Research runs that are already RUNNING on the server (adopted when a task view renders).
  // Server truth wins both ways: an active run keeps the controls disabled, and a freshly
  // released slot (worker finished / cancelled) re-enables them.
  function syncRunFromDetail(detail) {
    if (!detail) return;
    if (detail.is_running) {
      researchRunning = true;
      runningTaskId = detail.task_id;
    } else if (researchRunning && runningTaskId === detail.task_id) {
      researchRunning = false;
      runningTaskId = null;
    }
    syncRunCtl();
    if (activityEl) {
      activityEl.classList.toggle("running", researchRunning);
      const meta = activityEl._meta;
      if (meta && meta.dataset.hold !== "true") {
        meta.textContent = researchRunning ? "Running…" : "Idle";
      }
    }
  }

  // Stop is a cooperative cancel: the server sets cancel_requested, the current step finishes,
  // then the driver releases the slot and publishes run.cancelled (which the monitor turns
  // back into an is_running=false refresh).
  async function requestStop(taskId) {
    if (stopInFlight) return;
    stopInFlight = true;
    try {
      await apiFetch(`/research/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
      toast("Stop requested — finishing the current step…");
      activityNotice(taskId, "Stop requested — finishing the current step…");
    } catch (e) {
      toast(`Stop failed: ${e.message}`);
    } finally {
      stopInFlight = false;
    }
  }

  // A terminal run event from the monitor (run.finished / blocked / stalled / cancelled /
  // error) lands as an Activity notice so the user sees why the run stopped.
  function handleRunEvent(taskId, kind) {
    const verb = (kind || "run.?").replace("run.", "");
    const text = { finished: "Run finished", blocked: "Run paused", stalled: "Run stalled",
      cancelled: "Run stopped", error: "Run errored" }[verb] || `Run ${verb}`;
    activityNotice(taskId, `⏹ ${text}.`);
  }

  function activityNotice(taskId, text) {
    if (!activityEl || activityForTask !== taskId) return;
    const body = activityEl._body;
    if (!body) return;
    const empty = body.querySelector(".rtv-activity-empty");
    if (empty) empty.remove();
    body.appendChild(el("div", "rtv-activity-line info", text));
    if (body.children.length > 200) body.firstChild.remove();
    body.scrollTop = body.scrollHeight;
  }

  window.researchRunActive = (taskId, on) => {
    researchRunning = !!(taskId && on);
    if (on && taskId) runningTaskId = taskId;
    syncRunCtl();
    if (activityEl) {
      activityEl.classList.toggle("running", researchRunning);
      const meta = activityEl._meta;
      if (researchRunning && meta && meta.dataset.hold !== "true") meta.textContent = "Running…";
      // Run start reveals the collapsed feed so the user watches the live actions.
      if (researchRunning && activityForTask === taskId) setActivityCollapsed(activityEl, false);
    }
  };

  // Release a finished run only when it belongs to this task (server truth: a poll found this
  // task is no longer RUNNING). A different task's in-flight run is never cleared by it, so the
  // chip poll can safely re-enable a chained run without undoing another task's controls.
  window.researchReleaseIfIdle = (taskId) => {
    if (!researchRunning || runningTaskId !== taskId) return;
    researchRunning = false;
    runningTaskId = null;
    syncRunCtl();
    if (activityEl) {
      activityEl.classList.toggle("running", false);
      const meta = activityEl._meta;
      if (meta && meta.dataset.hold !== "true") meta.textContent = "Idle";
    }
  };

  // Activity header meta (status · stage) refreshed from the server status poll.
  window.researchActivityMeta = (status, stage) => {
    if (!activityEl) return;
    const parts = [];
    if (status) parts.push(status);
    if (stage) parts.push(`Stage ${stage}`);
    const meta = activityEl._meta;
    if (meta) {
      meta.textContent = parts.join(" · ") || "Idle";
      meta.dataset.hold = parts.length ? "true" : "false";
    }
  };

  function buildActivity() {
    // The Activity log is the workbench's fixed bottom bar (open by default). The header chevron
    // collapses it to just the header anytime; a run start (researchRunActive / ensureActivity)
    // re-expands it so the live actions are visible.
    const box = el("div", "research-section rtv-activity");
    const head = el("div", "rtv-activity-head");
    head.appendChild(el("span", "rtv-activity-dot"));
    head.appendChild(el("span", "rtv-activity-title", "Activity"));
    const meta = el("span", "rtv-activity-meta", "Idle");
    meta.dataset.hold = "false";
    head.appendChild(meta);
    head.appendChild(el("span", "rtv-activity-chev", "▾"));
    head.title = "Toggle the live activity feed";
    head.addEventListener("click", () => setActivityCollapsed(box, !box.classList.contains("collapsed")));
    box.appendChild(head);
    const body = el("div", "rtv-activity-body");
    body.appendChild(
      el("div", "rtv-activity-empty",
        "No run in progress — press ▶ Run in the task header or send a message in the task's session to start.")
    );
    box.appendChild(body);
    box._body = body;
    box._meta = meta;
    return box;
  }

  function setActivityCollapsed(box, collapsed) {
    if (!box) return;
    box.classList.toggle("collapsed", collapsed);
    const chev = box.querySelector(".rtv-activity-chev");
    if (chev) chev.textContent = collapsed ? "▸" : "▾";
  }

  function ensureActivity(taskId) {
    if (!activityEl || activityForTask !== taskId) {
      activityEl = buildActivity();
      activityForTask = taskId;
      // A run may already be in flight when the view is first opened (e.g. the user typed in
      // the session, then selected the task): reveal the feed so the live actions are visible.
      if (researchRunning && runningTaskId === taskId) setActivityCollapsed(activityEl, false);
    }
    return activityEl;
  }

  function clipText(s, n) {
    s = String(s ?? "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  window.researchActivityEvent = (evt) => {
    if (!activityEl) return;
    if (!evt || evt.type === "content") return; // final prose belongs to the chat
    // Only structured, user-meaningful moments reach the feed: server notices and tool
    // actions as short summaries. "thinking" (raw agent reasoning, "_handoff", "Let me try…")
    // and "step-answer" boundaries are internal detail — they live in the chat's collapsible
    // Thoughts box, never dumped here.
    let text = null;
    let kind = "";
    if (evt.type === "notice") { kind = "info"; text = clipText(evt.data, 160); }
    else if (evt.type === "tool") { kind = "tool"; text = activityToolLabel(evt.data && evt.data.name); }
    else if (evt.type === "done") { kind = "ok"; text = "✓ Turn finished."; }
    else return;
    const body = activityEl._body;
    if (!body) return;
    const empty = body.querySelector(".rtv-activity-empty");
    if (empty) empty.remove();
    const row = el("div", `rtv-activity-line ${kind}`);
    row.textContent = text;
    body.appendChild(row);
    if (body.children.length > 200) body.firstChild.remove();
    body.scrollTop = body.scrollHeight;
  };

  // ── top layer: task list (read-only, newest first) ───────────────────────
  // Render guard: two rapid loadTasks() calls (e.g. the create flow fires loadResearch() then
  // selectResearchTask() back-to-back) used to interleave — both cleared the container, then
  // both appended once their fetches resolved, doubling the list (A, B, A, B). A monotonically
  // increasing sequence number makes a stale render discard its result instead of appending.
  let loadTasksSeq = 0;
  async function loadTasks() {
    const seq = ++loadTasksSeq;
    if (!tasksList) return;
    tasksList.innerHTML = "";
    let tasks;
    try {
      tasks = (await apiFetch("/research/tasks")).tasks || [];
    } catch (e) {
      if (seq !== loadTasksSeq) return;
      tasksList.appendChild(el("div", "research-empty", `Tasks unavailable: ${e.message}`));
      return;
    }
    if (seq !== loadTasksSeq) return; // a newer load is in flight — drop this stale render
    if (!tasks.length) {
      tasksList.appendChild(
        el("div", "research-empty", "No tasks yet — create one with ＋ Research in the chat.")
      );
      return;
    }
    for (const t of tasks) {
      const row = el("div", "research-row");
      row.classList.toggle("selected", selectedTask && selectedTask.task_id === t.task_id);
      const main = el("div", "research-row-main");
      main.appendChild(el("div", "research-row-title", t.name || t.task_id));
      main.appendChild(el("div", "research-row-sub", `Stage ${t.stage} · ${t.status}${t.is_running ? " · RUNNING" : ""}${timeAgo(t.updated_at) ? ` · ${timeAgo(t.updated_at)}` : ""}`));
      row.appendChild(main);
      const del = el("button", "research-row-del", "🗑");
      del.title = "Delete task";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteTask(t);
      });
      row.appendChild(del);
      // Selecting a task is side-effect free: the chat opens the task's dedicated session
      // (loadStatus → openResearchSession) but nothing is sent — the model stays silent until
      // the user types a run instruction.
      row.addEventListener("click", () => {
        selectedTask = { task_id: t.task_id, name: t.name || t.task_id };
        loadTasks(); // re-render to move the selection highlight
        loadStatus(t.task_id); // opens the bound session in the chat + refreshes the status
        renderTaskView(); // main pane switches to this task's working directory
      });
      tasksList.appendChild(row);
    }
  }

  // Cascade delete: confirm first, then DELETE. The cloud task folder (materials/outputs/
  // task_spec.json/session_history.json) is moved to the Trash and the scratch state is
  // removed. A 409 (task RUNNING, or the report is in the Knowledge Base) keeps the task and
  // surfaces the server's reason verbatim.
  async function deleteTask(t) {
    if (researchRunning && t && t.task_id === runningTaskId) {
      toast("A research run is in progress — wait for it to finish before deleting the task.");
      return;
    }
    if (!window.confirmModal) return;
    const ok = await window.confirmModal({
      title: "Delete research task?",
      message: `Deleting "${t.name || t.task_id}" will remove the task and its state, and move its cloud folder (materials, outputs, task_spec.json, session_history.json) into the Trash. This cannot be undone.`,
      okLabel: "Delete",
      okClass: "danger",
    });
    if (!ok) return;
    try {
      await apiFetch(`/research/tasks/${encodeURIComponent(t.task_id)}`, { method: "DELETE" });
      toast("Task deleted.");
      if (selectedTask && selectedTask.task_id === t.task_id) {
        selectedTask = null;
        window.currentResearchTask = null;
        stopMonitor(); // the deleted task's live stream is gone
        if (statusBody) statusBody.innerHTML = "";
        // Drop the main-pane task view and restore the empty-state guide card.
        const pane = document.getElementById("research-task-view");
        if (pane) { pane.classList.add("hidden"); pane.innerHTML = ""; }
        const guide = document.getElementById("research-guide");
        if (guide) guide.classList.remove("hidden");
      }
      loadTasks();
    } catch (err) {
      toast(`Delete failed: ${err.message}`);
      loadTasks(); // re-render in case the row state changed
    }
  }

  // ── bottom layer: selected task status (read-only) ───────────────────────
  async function loadStatus(taskId) {
    if (!statusBody) return;
    statusBody.innerHTML = "";
    statusBody.appendChild(el("div", "research-status-empty", "Loading…"));
    let detail;
    try {
      detail = await apiFetch(`/research/tasks/${encodeURIComponent(taskId)}`);
    } catch (e) {
      statusBody.innerHTML = "";
      statusBody.appendChild(el("div", "research-status-empty", `Status unavailable: ${e.message}`));
      return;
    }
    renderStatusDetail(detail);
  }

  // Render the left status panel from an already-fetched authoritative detail. Used by both the
  // initial load and the live revision monitor's coalesced refetch, so the two never double-fetch.
  function renderStatusDetail(detail, opts = {}) {
    syncRunFromDetail(detail);
    recordResearchSession(detail);
    // Open this task's dedicated session in the chat — silent, no message sent. This is what
    // makes task selection side-effect free (app.js openResearchSession); a repeated open of
    // the same session is a no-op, so it never forks a new one. Live-monitor refetches pass
    // openSession:false — the user may be mid-conversation elsewhere, and a background refresh
    // must not yank them into the task's session.
    if (opts.openSession !== false && window.openResearchSession) {
      window.openResearchSession(detail.task_id, detail.name || detail.task_id, detail.session_id || null);
    }
    if (!statusBody) return;
    statusBody.innerHTML = "";
    const head = el("div", "research-status-head");
    head.appendChild(el("div", "research-status-name", detail.name || detail.task_id));
    const sub = el("div", "research-status-sub", `${detail.status}${detail.is_running ? " — running" : ""}${detail.description ? ` — ${detail.description}` : ""}`);
    head.appendChild(sub);
    statusBody.appendChild(head);
    const banner = renderLastBlock(detail);
    if (banner) statusBody.appendChild(banner);
    statusBody.appendChild(renderStageNodes(detail.stage));
    const gates = renderGates(detail.gates || {});
    if (gates) statusBody.appendChild(gates);
    const nodes = renderNodes(detail.nodes || {});
    if (nodes) statusBody.appendChild(nodes);
    syncRunCtl(); // reflect any run state on the freshly rendered Run control
    // Surface any human-gate decision (pending override) as an inline Approve / Reject card
    // in the task's chat; app.js decides whether that session is the one on screen.
    if (window.showResearchGateCard) {
      window.showResearchGateCard({
        task_id: detail.task_id,
        name: detail.name || detail.task_id,
        session_id: detail.session_id || null,
        pending_overrides: detail.pending_overrides || [],
      });
    }
  }

  // Discrete stage nodes: done ✓ / current ● / pending ○, chained with arrows.
  function renderStageNodes(stage) {
    const box = section("Stage");
    const bar = el("div", "research-stage-bar");
    STAGES.forEach((s, i) => {
      if (i > 0) bar.appendChild(el("span", "research-stage-arrow", "➔"));
      const state = s === stage ? "current" : (STAGES.indexOf(stage) > i ? "done" : "pending");
      const pill = el("span", `research-stage-pill ${state}`);
      pill.appendChild(el("span", "research-stage-mark", state === "done" ? "✓" : (state === "current" ? "●" : "○")));
      pill.appendChild(el("span", "research-stage-label", s));
      bar.appendChild(pill);
    });
    box.appendChild(bar);
    return box;
  }

  function renderGates(gates) {
    const entries = Object.entries(gates).filter(([name]) => GATE_NAMES[name]);
    if (!entries.length) return null;
    const box = section("Gates");
    const wrap = el("div", "research-gates");
    for (const [name, status] of entries) {
      const chip = el("span", `research-gate-chip ${status === "PASS" || status === "OVERRIDE" ? "pass" : "pending"}`, `${GATE_NAMES[name]}: ${status}`);
      wrap.appendChild(chip);
    }
    box.appendChild(wrap);
    return box;
  }

  function renderNodes(nodes) {
    const groups = { Source: [], Claim: [], Evidence: [] };
    let count = 0;
    for (const type of Object.keys(groups)) {
      for (const n of nodes[type] || []) {
        groups[type].push(n);
        count++;
      }
    }
    if (!count) return null;
    const box = section("Evidence graph");
    const wrap = el("div", "research-graph");
    for (const [type, list] of Object.entries(groups)) {
      if (!list.length) continue;
      const group = el("div", "research-graph-group");
      group.appendChild(el("div", "research-graph-group-title", type));
      for (const n of list) {
        const chip = el("span", `research-node research-node-${type.toLowerCase()}`, n.label || n.id);
        chip.title = `${n.id} · ${n.status || "VALID"}`;
        group.appendChild(chip);
      }
      wrap.appendChild(group);
    }
    box.appendChild(wrap);
    return box;
  }

  function formatBytes(n) {
    if (n === null || n === undefined) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1048576).toFixed(1)} MB`;
  }

  // Banner for the most recent terminal run outcome (project["last_block"]): the status pane
  // and main task view show it until the next run starts, so a finished/blocked/stalled/
  // cancelled run is never silently unexplained on re-open.
  function renderLastBlock(detail) {
    const lb = detail.last_block;
    if (!lb || !lb.kind) return null;
    const kind = lb.kind === "finished" ? "finished"
      : lb.kind === "blocked" ? "paused"
      : lb.kind === "stalled" ? "stalled"
      : lb.kind === "cancelled" ? "stopped" : "errored";
    const label = { finished: "✓ Finished", paused: "⏸ Paused", stalled: "⟳ Stalled",
      stopped: "⏹ Stopped", errored: "✗ Error" }[kind];
    const box = el("div", `research-status-banner rtv-banner ${kind}`);
    box.appendChild(el("div", "research-status-banner-tag", label));
    if (lb.reason) box.appendChild(el("div", "research-status-banner-text", lb.reason));
    return box;
  }

  // ── main pane: selected task's working-directory view ────────────────────
  // Clicking a task in the left list switches the middle main pane to that task's cloud
  // folder: title + details, a Run action into its dedicated session (auto-starts the run),
  // and the full working-directory file list (task_spec.json / session_history.json at the
  // root, materials/, outputs/), each file clickable to view its content and material status.
  // A live Activity feed sits below the tree while a run is in progress.
  async function renderTaskView() {
    const pane = document.getElementById("research-task-view");
    const guide = document.getElementById("research-guide");
    if (!pane) return;
    window.currentResearchTask = selectedTask;
    // The guide card belongs to the empty desktop only; as soon as a task is selected the
    // main pane switches to the task view and the guide is hidden (never shown side by side).
    if (guide) guide.classList.toggle("hidden", !!selectedTask);
    if (!selectedTask) {
      pane.classList.add("hidden");
      stopMonitor();
      return;
    }
    pane.classList.remove("hidden");
    pane.innerHTML = "";
    pane.appendChild(el("div", "research-status-empty", "Loading…"));
    let detail;
    try {
      detail = await apiFetch(`/research/tasks/${encodeURIComponent(selectedTask.task_id)}`);
    } catch (e) {
      pane.innerHTML = "";
      pane.appendChild(el("div", "research-status-empty", `Task unavailable: ${e.message}`));
      return;
    }
    renderMainDetail(detail);
    // The live monitor's revision floor is what we just rendered: the server snapshots after
    // subscribing, so only a change that committed between this fetch and the subscribe (or
    // later) refetches — a change in that gap is never silently missed.
    monitorRevision = detail.project_revision || 0;
    startMonitor(selectedTask.task_id);
  }

  // Render the main-pane task view (title + actions card, terminal-outcome banner, working-
  // directory tree, live Activity feed) from an authoritative detail. Shared by the initial
  // selection and the live monitor's coalesced refetch, so an in-flight run re-renders in
  // place — the Activity node survives (ensureActivity) and the tree keeps its expansion state
  // (treeOpenState) across those re-renders.
  function renderMainDetail(detail) {
    const pane = document.getElementById("research-task-view");
    if (!pane) return;
    syncRunFromDetail(detail);
    recordResearchSession(detail);
    pane.innerHTML = "";

    // Card header: title + status/stage badges + actions up top, working-directory meta as
    // the card's footer strip — everything about the task in one visual block.
    const card = el("div", "rtv-card");
    const head = el("div", "rtv-card-head");
    const titleRow = el("div", "rtv-title-row");
    titleRow.appendChild(el("div", "rtv-title", detail.name || detail.task_id));
    titleRow.appendChild(el("span", `rtv-chip ${detail.status === "ACTIVE" ? "active" : ""}`, detail.status));
    titleRow.appendChild(el("span", "rtv-chip stage", `Stage ${detail.stage}`));
    head.appendChild(titleRow);
    const actions = el("div", "rtv-actions");
    // Run starts (or resumes) the task's run in one click; Stop requests the cooperative cancel
    // of the in-flight run; Delete cascades the cloud folder + state. Run and Delete disable
    // while a run is in flight (researchRunning), Run switches to its running style, and Stop is
    // its mirror — only actionable while the run is live.
    const openBtn = el("button", "run-ctl run-start", runStartText());
    openBtn.addEventListener("click", () =>
      window.startResearchRun(detail.task_id, detail.name || detail.task_id, detail.session_id)
    );
    actions.appendChild(openBtn);
    const stopBtn = el("button", "run-ctl run-stop", "⏹ Stop");
    stopBtn.title = "Stop the running research (finishes the current step first)";
    stopBtn.addEventListener("click", () => requestStop(detail.task_id));
    actions.appendChild(stopBtn);
    const delBtn = el("button", "ghost rtv-del run-ctl", "🗑 Delete task");
    delBtn.addEventListener("click", () => deleteTask({ task_id: detail.task_id, name: detail.name || detail.task_id }));
    actions.appendChild(delBtn);
    head.appendChild(actions);
    card.appendChild(head);
    if (detail.description) {
      const desc = el("div", "rtv-desc", detail.description);
      card.appendChild(desc);
    }
    const meta = el("div", "rtv-meta");
    const wd = el("div", "rtv-meta-item");
    wd.appendChild(el("span", "", "📁"));
    wd.appendChild(el("span", "path", detail.cloud_folder_path || "(no cloud folder)"));
    meta.appendChild(wd);
    meta.appendChild(el("div", "rtv-meta-item", `Updated ${timeAgo(detail.updated_at)}`));
    card.appendChild(meta);
    pane.appendChild(card);

    // The most recent terminal run outcome (finished/blocked/stalled/stopped/error) reads as a
    // banner under the card until the next run starts.
    const banner = renderLastBlock(detail);
    if (banner) pane.appendChild(banner);

    // Two-zone workbench: the top area holds the Working directory tree (left) next to a file
    // preview (right), and the Activity log is pinned as a fixed bottom bar. The pane itself
    // never grows — every zone scrolls internally (the tree and preview body have their own
    // overflow-y, the Activity log scrolls inside its fixed height).
    const workbench = el("div", "rtv-workbench");
    const top = el("div", "rtv-work-top");
    top.appendChild(renderTreeCol(detail));
    top.appendChild(ensurePreviewCol(detail));
    workbench.appendChild(top);
    workbench.appendChild(ensureActivity(detail.task_id));
    pane.appendChild(workbench);
    reconcilePreview(detail); // live-refresh the open file when the agent rewrote it
    syncRunCtl(); // reflect any run state on the freshly rendered Run / Delete controls
  }

  // The task folder as a standard VS Code / Explorer-style vertical tree — the left column of
  // the workbench's top area. Rows reuse the Cloud Drive .cd-* classes; children nest in
  // .rtv-kids containers whose dotted border-left is the per-level indent guide. The task
  // folder row opens into its subfolders (materials/, outputs/, any extra) and the root mirrors
  // (task_spec.json / session_history.json). Folder rows start collapsed but the task row is
  // expanded one level; ▸/▾ toggles expand in place and each file row opens in the preview
  // column to the right. The expansion set is kept per task across live re-renders.
  function renderTreeCol(detail) {
    const col = el("div", "rtv-col rtv-tree-col");
    const head = el("div", "rtv-col-head");
    head.appendChild(el("span", "rtv-col-title", "Working directory"));
    const fileCount = (detail.cloud_files || []).length;
    head.appendChild(el("span", "rtv-col-count", `(${fileCount} file${fileCount === 1 ? "" : "s"})`));
    col.appendChild(head);
    const wrap = el("div", "rtv-tree-scroll");
    col.appendChild(wrap);
    const cloudPath = detail.cloud_folder_path || "";
    const groups = new Map();
    for (const f of detail.cloud_files || []) {
      let sub = f.folder_path || "";
      if (sub.startsWith(cloudPath + "/")) sub = sub.slice(cloudPath.length + 1);
      else sub = sub === cloudPath ? "" : sub;
      if (!groups.has(sub)) groups.set(sub, []);
      groups.get(sub).push(f);
    }
    const roots = groups.get("") || [];
    const extra = Array.from(groups.keys()).filter((k) => k && k !== "materials" && k !== "outputs");
    const segs = ["materials", "outputs", ...extra];
    const open = treeOpenState.get(detail.task_id) || new Set([""]);
    treeOpenState.set(detail.task_id, open);

    function toggle(seg) {
      if (open.has(seg)) open.delete(seg);
      else open.add(seg);
      const treeEl = wrap.querySelector(".rtv-tree");
      if (treeEl) treeEl.replaceWith(buildTree());
    }

    // One clickable file row: type icon + name on the left, size (and Knowledge Base tag) on
    // the right — .cd-name flex:1 pushes the meta to the far edge, like the Cloud Drive rows.
    function fileRow(f) {
      const row = el("div", "cd-row rtv-tree-file");
      if (previewState.file && previewState.taskId === detail.task_id && previewState.file.id === f.id) {
        row.classList.add("selected");
      }
      row.appendChild(el("span", "cd-tw"));
      row.appendChild(el("span", "cd-icon", fileTypeIcon(f)));
      const name = el("span", "cd-name", f.name);
      row.appendChild(name);
      row.appendChild(el("span", "cd-meta", formatBytes(f.size)));
      if (f.rag_status === "INDEXED") row.appendChild(el("span", "rtv-rag-tag", "KB"));
      else if (f.rag_status === "PENDING") row.appendChild(el("span", "rtv-rag-tag pending", "…"));
      row.title = `${f.folder_path ? f.folder_path + "/" : ""}${f.name}`;
      row.addEventListener("click", () => previewFile(detail.task_id, f));
      return row;
    }

    // A folder row: ▸/▾ toggles its children. The count (n) hangs right after the name and,
    // for empty folders, a grey "(empty)" tag — all on the same line (no separate empty row).
    function folderRow(seg) {
      const files = groups.get(seg) || [];
      const isOpen = open.has(seg);
      const row = el("div", "cd-row cd-folder rtv-folder-row");
      const tw = el("span", "cd-tw", files.length ? (isOpen ? "▾" : "▸") : "");
      row.appendChild(tw);
      row.appendChild(el("span", "cd-icon", seg === "" ? "🗂" : "📁"));
      row.appendChild(el("span", "cd-name", seg === "" ? (cloudPath || "Task folder") : seg));
      row.appendChild(el("span", "rtv-count", `(${files.length})`));
      if (!files.length) row.appendChild(el("span", "rtv-empty-tag", "(empty)"));
      row.title = seg === "" ? cloudPath : seg;
      if (files.length) {
        tw.addEventListener("click", (e) => { e.stopPropagation(); toggle(seg); });
        row.addEventListener("click", () => toggle(seg));
      }
      return row;
    }

    // One nesting level: its own dotted guide line (border-left) + children.
    function kidsOf() {
      const kids = el("div", "rtv-kids");
      for (const seg of segs) {
        kids.appendChild(folderRow(seg));
        const files = groups.get(seg) || [];
        if (open.has(seg) && files.length) {
          const sub = el("div", "rtv-kids");
          for (const f of files.slice(0, 50)) sub.appendChild(fileRow(f));
          kids.appendChild(sub);
        }
      }
      // Root mirrors (task_spec.json / session_history.json) sit at the folder root.
      for (const f of roots.slice(0, 50)) kids.appendChild(fileRow(f));
      return kids;
    }

    function buildTree() {
      const treeEl = el("div", "rtv-tree");
      treeEl.appendChild(folderRow(""));
      if (open.has("")) treeEl.appendChild(kidsOf());
      return treeEl;
    }

    wrap.appendChild(buildTree());
    return col;
  }

  // Pick a small icon by file type (mime first, then extension) so the file list reads at a
  // glance; falls back to a generic document.
  function fileTypeIcon(f) {
    const mime = f.mime_type || "";
    if (mime.startsWith("image/")) return "🖼";
    if (mime.startsWith("video/")) return "🎞";
    if (mime.startsWith("audio/")) return "🎵";
    if (mime.includes("pdf")) return "📕";
    if (mime.includes("spreadsheet") || mime.includes("csv")) return "📊";
    const ext = (f.name || "").split(".").pop().toLowerCase();
    if (["md", "markdown"].includes(ext)) return "📝";
    if (["py", "js", "ts", "go", "rs", "java", "c", "cpp", "sql"].includes(ext)) return "⌨";
    if (["csv", "xlsx", "xls"].includes(ext)) return "📊";
    if (["json", "yaml", "yml", "toml"].includes(ext)) return "⚙";
    if (["pdf"].includes(ext)) return "📕";
    return "📄";
  }

  // ── workbench file preview (right column of the top area) ────────────────
  // The preview panel persists per task: monitor re-renders re-attach the same node, so an open
  // file is not wiped every refresh. Clicking a tree row loads the file into it; when the
  // monitor refetches and sees the open file grew (the agent rewrote it), the content refreshes
  // in place (throttled) so the pane reads the latest state without a manual re-click.
  function buildPreviewCol() {
    const col = el("div", "rtv-col rtv-preview-col");
    const head = el("div", "rtv-col-head");
    head.appendChild(el("span", "rtv-col-title", "File preview"));
    head.appendChild(el("span", "rtv-col-hint", "click a file to preview"));
    col.appendChild(head);
    const body = el("div", "rtv-preview-body");
    body.appendChild(
      el("div", "rtv-preview-placeholder",
        "Select a file in the Working directory to preview its contents here.")
    );
    col.appendChild(body);
    col._title = head.querySelector(".rtv-col-title");
    col._body = body;
    return col;
  }

  function ensurePreviewCol(detail) {
    // A different task starts a fresh panel; same-task re-renders keep the existing node (and
    // whatever content is already loaded in it).
    if (!previewColEl || previewColEl._taskId !== detail.task_id) {
      previewColEl = buildPreviewCol();
      previewColEl._taskId = detail.task_id;
      previewTitleEl = previewColEl._title;
      previewBodyEl = previewColEl._body;
      previewState = { taskId: detail.task_id, file: null, size: null };
      previewRefreshedAt = 0;
    }
    return previewColEl;
  }

  function previewPlaceholder(text) {
    if (!previewBodyEl) return;
    previewBodyEl.innerHTML = "";
    previewBodyEl.appendChild(el("div", "rtv-preview-placeholder", text));
    previewBodyEl.scrollTop = 0;
  }

  async function previewFile(taskId, f) {
    if (!previewBodyEl || !previewColEl || previewColEl._taskId !== taskId) return;
    previewState = { taskId, file: f, size: f.size != null ? f.size : null };
    if (previewTitleEl) {
      previewTitleEl.textContent = `${f.folder_path ? f.folder_path + "/" : ""}${f.name}`;
    }
    previewPlaceholder("Loading…");
    let res;
    try {
      res = await apiFetch(`/files/${encodeURIComponent(f.id)}/content`);
    } catch (e) {
      if (previewState.file && previewState.file.id === f.id) previewPlaceholder(`Content unavailable: ${e.message}`);
      return;
    }
    if (!previewState.file || previewState.file.id !== f.id) return; // a newer selection won
    if (!previewBodyEl) return;
    previewBodyEl.innerHTML = "";
    const doc = el("div", "rtv-preview-doc");
    doc.innerHTML = renderMarkdown(res.content);
    previewBodyEl.appendChild(doc);
    previewBodyEl.scrollTop = 0;
  }

  // After a monitor refetch, live-refresh the open file if the agent rewrote it (size changed).
  function reconcilePreview(detail) {
    if (!previewState.file || previewState.taskId !== detail.task_id) return;
    if (!previewBodyEl) return;
    const cur = (detail.cloud_files || []).find((f) => f.id === previewState.file.id);
    if (!cur) {
      previewState = { taskId: detail.task_id, file: null, size: null };
      if (previewTitleEl) previewTitleEl.textContent = "File preview";
      previewPlaceholder("This file is no longer in the task folder.");
      return;
    }
    const sz = cur.size != null ? cur.size : null;
    if (sz != null && previewState.size != null && sz !== previewState.size) {
      const now = Date.now();
      if (now - previewRefreshedAt > 1200) {
        previewRefreshedAt = now;
        previewFile(detail.task_id, cur);
      }
    }
  }

  // ── live revision monitor ────────────────────────────────────────────────
  // One SSE stream per selected task. The server endpoint subscribes to the task's Redis wake-up
  // channel *before* snapshotting, so a change committing in the subscribe→snapshot gap is never
  // missed. The stream is an invalidation hint, not the data: a snapshot seeds the revision
  // floor; a change whose project_revision strictly exceeds the last one applied schedules one
  // coalesced refetch of the authoritative status (never a blind timer). Terminal run events
  // also land an Activity notice so a run's stop is never unexplained.
  const TERMINAL_RUN_KINDS = new Set([
    "run.finished", "run.blocked", "run.stalled", "run.cancelled", "run.error",
  ]);

  function startMonitor(taskId) {
    stopMonitor();
    monitorTaskId = taskId;
    const abort = new AbortController();
    monitorAbort = abort;
    const token = bearerToken();
    if (!token) return; // guests have no tasks — nothing to watch
    (async () => {
      try {
        const res = await fetch(`/api/research/tasks/${encodeURIComponent(taskId)}/monitor`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: abort.signal,
        });
        if (!res.ok) return;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
          let idx;
          while ((idx = buf.indexOf("\n\n")) !== -1) {
            handleMonitorBlock(buf.slice(0, idx));
            buf = buf.slice(idx + 2);
          }
        }
        if (buf.trim()) handleMonitorBlock(buf);
      } catch { /* aborted on task switch / network drop — the next selection restarts it */ }
      finally {
        if (monitorAbort === abort) monitorAbort = null;
      }
    })();
  }

  function stopMonitor() {
    monitorTaskId = null;
    refreshQueued = false;
    if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
    if (monitorAbort) { monitorAbort.abort(); monitorAbort = null; }
  }

  function handleMonitorBlock(block) {
    // SSE frames: comment lines (": keep-alive") and the "data:" JSON payload. Only the data
    // frame matters — heartbeats and stray frames are ignored.
    let raw = null;
    for (const line of block.split("\n")) {
      const l = line.trim();
      if (l.startsWith("data:")) raw = l.slice(5).trim();
    }
    if (raw == null) return;
    let evt;
    try { evt = JSON.parse(raw); } catch { return; }
    if (evt.type === "snapshot") {
      // (Re)connecting: the server's current revision is the new floor. If it is ahead of what
      // we last rendered, a change landed between our fetch and the server's subscribe —
      // reconcile with one authoritative refetch so we never sit on a stale view.
      const rev = Number(evt.project_revision || 0);
      if (rev > monitorRevision && monitorTaskId) scheduleRefresh(monitorTaskId);
      monitorRevision = rev;
      return;
    }
    if (evt.type !== "change") return;
    const rev = Number(evt.project_revision || 0);
    if (rev <= monitorRevision) return; // stale / duplicate hint — already applied
    monitorRevision = rev;
    const kind = evt.kind || "";
    if (TERMINAL_RUN_KINDS.has(kind)) handleRunEvent(monitorTaskId, kind);
    if (monitorTaskId) scheduleRefresh(monitorTaskId);
  }

  // Coalesced authoritative refetch: a burst of change hints (e.g. a tool round touching several
  // artifacts) collapses into at most one fetch every ~300ms.
  function scheduleRefresh(taskId) {
    if (refreshQueued) return;
    refreshQueued = true;
    refreshTimer = setTimeout(() => {
      refreshQueued = false;
      refreshTimer = null;
      refreshTaskNow(taskId);
    }, 300);
  }

  async function refreshTaskNow(taskId) {
    if (taskId !== monitorTaskId) return;
    if (!selectedTask || selectedTask.task_id !== taskId) return; // the user switched tasks
    let detail;
    try {
      detail = await apiFetch(`/research/tasks/${encodeURIComponent(taskId)}`);
    } catch { return; } // transient — the next change hint retries
    if (taskId !== monitorTaskId) return;
    // Refresh both research surfaces without re-opening the chat session (openSession:false) —
    // the user may be in another chat; a background refresh must not yank them away.
    renderStatusDetail(detail, { openSession: false });
    const pane = document.getElementById("research-task-view");
    if (pane && !pane.classList.contains("hidden")) renderMainDetail(detail);
  }

  // ── navigation / refresh ─────────────────────────────────────────────────
  window.loadResearch = () => {
    loadTasks();
    if (selectedTask) {
      loadStatus(selectedTask.task_id);
      renderTaskView();
    }
  };

  // Jump to a specific task (used right after "＋ Research" creates one, so the new task is
  // highlighted in the list, its stage shows in the status pane, and the main pane switches
  // to its working directory while the chat drives it).
  window.selectResearchTask = (taskId, name) => {
    selectedTask = { task_id: taskId, name: name || taskId };
    loadTasks();
    loadStatus(taskId);
    renderTaskView();
  };
})();
