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

  // A task's execution mode (strict | progressive) is fixed at creation; the UI only echoes it
  // (locked badge next to Run, and the 🔬 chat sub-bar badge). Shared wording for every surface.
  const RESEARCH_MODE_INFO = {
    strict: {
      label: "Strict",
      tip: "Strict mode — a failed gate pauses the run for a human override. Locked at creation.",
    },
    progressive: {
      label: "Progressive",
      tip: "Progressive mode — a failed gate is recorded as a diagnostic and the run continues. Locked at creation.",
    },
  };
  function modeInfo(mode) {
    return RESEARCH_MODE_INFO[mode === "progressive" ? "progressive" : "strict"];
  }

  function recordResearchSession(detail) {
    if (detail && detail.session_id) {
      window.researchSessions.set(detail.session_id, {
        task_id: detail.task_id,
        name: detail.name || detail.task_id,
        stage: detail.stage || null,
        status: detail.status || null,
        execution_mode: detail.execution_mode || "strict",
      });
      if (window.updateResearchChip) window.updateResearchChip();
    }
  }

  // Canonical 10-stage chain (mirrors plugins/research/plugin.py ``_STAGES``): every research task
  // advances DISCOVER -> … -> PUBLISH, so the ladder must list all ten — a truncated array would
  // leave later stages (DESIGN / EXPLAIN / REVIEW / REPRODUCE) with no node to highlight.
  const STAGES = [
    "DISCOVER", "FRAME", "EVIDENCE", "DESIGN", "EXECUTE",
    "EXPLAIN", "WRITE", "REVIEW", "REPRODUCE", "PUBLISH",
  ];
  // Human labels for the deterministic gates the state machine can record (plugins/research/plugin.py
  // ``_GATES``). Only gates present in a task's status dict are rendered.
  const GATE_NAMES = {
    DESIGN_GATE: "Design Gate",
    EVIDENCE_GATE: "Evidence Gate",
    CLAIM_GATE: "Claim Gate",
    QUALITY_GATE: "Quality Gate",
  };

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
  // Per-task run bookkeeping: task_ids the client currently believes are RUNNING. The
  // backend permits several tasks to run concurrently (T4), so a single global
  // ``researchRunning`` flag used to bleed one task's "Running…" / disabled state onto every
  // other task's header controls — the "start a task → all tasks look green / this one won't
  // start" symptom. Keyed by task_id, each view reflects only its own task's run state.
  const runningTasks = new Set();
  const isTaskRunning = (taskId) => !!taskId && runningTasks.has(taskId);
  let activityEl = null;         // live Activity section (survives same-task pane re-renders)
  let activityForTask = null;    // task_id the current activity feed belongs to
  // Live revision monitor (short-poll while a task is selected): each tick is a plain GET of
  // the task detail whose project_revision is compared against the last rendered floor, so the
  // desktop refetches the authoritative status exactly when something changed. No persistent
  // stream — the old SSE /monitor design leaked upstream sockets on every task switch (Electron
  // 31 does not propagate protocol-request cancellation to the main-process proxy), saturating
  // Chromium's 6-per-origin HTTP/1.1 pool and hanging every later /api fetch behind "Loading…".
  let monitorTaskId = null;
  let monitorRevision = 0;       // last applied project_revision (stale polls ignored)
  let monitorPollTimer = null;   // pending poll timeout (cleared by stopMonitor)
  let monitorWasRunning = false; // previous poll's is_running (transition → Activity notice)
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
  let previewFsEscBound = false;  // Esc-to-exit-fullscreen listener attached once
  // Evidence-graph detail drawer: the status panel only shows compact counts + a "View graph"
  // button; the full Source–Evidence–Claim mapping renders in a right-hand drawer. The body is
  // always rebuilt from the freshest task detail (never cached across renders), so an edition
  // reset (graph emptied server-side) or a task switch closes it and the next open is clean.
  let graphDrawerOpen = false;    // drawer is visible (slide-in shown)
  let graphDrawerTaskId = null;   // task whose graph the drawer is showing (switch/reset → close)
  let graphDrawerSig = null;      // signature of the graph the open drawer was rendered from
  let graphDrawerBound = false;   // scrim / close-button / Esc listeners attached once

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
  // The header controls always belong to the currently selected task, so "running" is that
  // task's own state — never another task's in-flight run.
  function runStartText() {
    return isTaskRunning(selectedTask && selectedTask.task_id) ? "● Running…" : "▶ Run";
  }

  function syncRunCtl() {
    const viewRunning = isTaskRunning(selectedTask && selectedTask.task_id);
    document.querySelectorAll(".run-ctl").forEach((btn) => {
      if (btn.classList.contains("run-stop")) {
        // Stop is the mirror of Run: only actionable while THIS task's run is in flight.
        btn.disabled = !viewRunning;
      } else if (btn.classList.contains("run-start")) {
        btn.disabled = viewRunning;
        btn.textContent = viewRunning ? "● Running…" : "▶ Run";
        btn.classList.toggle("running", viewRunning);
      } else {
        btn.disabled = viewRunning; // Delete Task / other run-scoped controls
      }
    });
  }

  // Research runs that are already RUNNING on the server (adopted when a task view renders).
  // Server truth wins both ways: an active run keeps the controls disabled, and a freshly
  // released slot (worker finished / cancelled) re-enables them. Per-task: another task's run
  // is never cleared or asserted by this task's snapshot.
  function syncRunFromDetail(detail) {
    if (!detail) return;
    if (detail.is_running) runningTasks.add(detail.task_id);
    else runningTasks.delete(detail.task_id);
    syncRunCtl();
    if (activityEl && activityForTask === detail.task_id) {
      const running = isTaskRunning(detail.task_id);
      activityEl.classList.toggle("running", running);
      const meta = activityEl._meta;
      if (meta && meta.dataset.hold !== "true") {
        meta.textContent = running ? "Running…" : "Idle";
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

  // (Terminal-run notices are derived in pollMonitor from the running→idle transition.)

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
    if (!taskId) return;
    if (on) runningTasks.add(taskId); else runningTasks.delete(taskId);
    // A Run click restarts the task server-side (begin_run bumps the project revision). Restart
    // the poll loop so it immediately catches that bump with the fast running interval, and
    // refetch the authoritative status right away. Without this the status card keeps showing
    // the stale Finished view until a manual reload.
    if (on && monitorTaskId === taskId) {
      scheduleRefresh(taskId);
      if (monitorPollTimer) clearTimeout(monitorPollTimer);
      monitorPollTimer = setTimeout(pollMonitor, 0);
    }
    syncRunCtl();
    if (activityEl && activityForTask === taskId) {
      activityEl.classList.toggle("running", on);
      const meta = activityEl._meta;
      if (on && meta && meta.dataset.hold !== "true") meta.textContent = "Running…";
      // Run start reveals the collapsed feed so the user watches the live actions.
      if (on) setActivityCollapsed(activityEl, false);
    }
  };

  // Release a finished run only when it belongs to this task (server truth: a poll found this
  // task is no longer RUNNING). A different task's in-flight run is never cleared by it, so the
  // chip poll can safely re-enable a chained run without undoing another task's controls.
  window.researchReleaseIfIdle = (taskId) => {
    if (!isTaskRunning(taskId)) return;
    runningTasks.delete(taskId);
    syncRunCtl();
    if (activityEl && activityForTask === taskId) {
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
      if (isTaskRunning(taskId)) setActivityCollapsed(activityEl, false);
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
      // A transient backend/db blip left the list stuck with no way back; give a manual retry.
      const retry = el("button", "ghost research-empty", "↻ Retry");
      retry.addEventListener("click", () => loadTasks());
      tasksList.appendChild(retry);
      return;
    }
    if (seq !== loadTasksSeq) return; // a newer load is in flight — drop this stale render
    if (!tasks.length) {
      tasksList.appendChild(
        el("div", "research-empty", "No tasks yet — create one with ＋ New Research above.")
      );
      return;
    }
    for (const t of tasks) {
      const row = el("div", "research-row");
      row.classList.toggle("selected", selectedTask && selectedTask.task_id === t.task_id);
      const main = el("div", "research-row-main");
      main.appendChild(el("div", "research-row-title", t.name || t.task_id));
      main.appendChild(el("div", "research-row-sub", `Stage ${t.stage} · ${t.status}${t.is_running ? " · RUNNING" : ""}${t.is_running && t.run_stale ? " · STALE" : ""}${timeAgo(t.updated_at) ? ` · ${timeAgo(t.updated_at)}` : ""}`));
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
    if (t && isTaskRunning(t.task_id)) {
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
        stopMonitor(); // the deleted task's poll loop is gone
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
    await renderStatusDetail(detail);
  }

  // Render the left status panel from an already-fetched authoritative detail. Used by both the
  // initial load and the live revision monitor's coalesced refetch, so the two never double-fetch.
  async function renderStatusDetail(detail, opts = {}) {
    syncRunFromDetail(detail);
    recordResearchSession(detail);
    // Open this task's dedicated session in the chat — silent, no message sent. This is what
    // makes task selection side-effect free (app.js openResearchSession); a repeated open of
    // the same session is a no-op, so it never forks a new one. Live-monitor refetches pass
    // openSession:false — the user may be mid-conversation elsewhere, and a background refresh
    // must not yank them into the task's session.
    if (opts.openSession !== false && window.openResearchSession) {
      // Await the session open: it rebuilds the chat from the session DB (and settles
      // ``state.sessionId``) before the gate card / note logic below decides whether this task's
      // session is the one on screen. Without the await, a first open of an already-parked task
      // sees the *previous* session id and skips both the note reload and the Approve/Reject card.
      await window.openResearchSession(detail.task_id, detail.name || detail.task_id, detail.session_id || null);
    }
    if (!statusBody) return;
    statusBody.innerHTML = "";
    const head = el("div", "research-status-head");
    head.appendChild(el("div", "research-status-name", detail.name || detail.task_id));
    const sub = el("div", "research-status-sub", `${detail.status}${detail.is_running ? " — running" : ""}${detail.is_running && detail.run_stale ? " — stale lease" : ""}${detail.description ? ` — ${detail.description}` : ""}`);
    if (detail.is_running && detail.run_stale) {
      sub.title = "Lease heartbeat lapsed: the executor crashed; a waking or arriving job reclaims the iteration (F3 read-only badge — no manual repair needed)";
    }
    head.appendChild(sub);
    statusBody.appendChild(head);
    const banner = renderLastBlock(detail);
    if (banner) statusBody.appendChild(banner);
    statusBody.appendChild(renderStageNodes(detail.stage));
    const gates = renderGates(detail.gates || {});
    if (gates) statusBody.appendChild(gates);
    const graphCard = renderEvidenceSummary(detail);
    if (graphCard) statusBody.appendChild(graphCard);
    syncGraphDrawer(detail); // keep an open drawer live / zeroed against this fresh detail
    syncRunCtl(); // reflect any run state on the freshly rendered Run control
    // Surface any human-gate decision (pending override) as an inline Approve / Reject card
    // in the task's chat; app.js decides whether that session is the one on screen.
    if (window.researchReloadGateNotes) {
      // When the run just parked at a gate, the backend has already committed its deterministic
      // ``system`` note to the session DB — pull it into the live chat before the card renders.
      await window.researchReloadGateNotes({
        task_id: detail.task_id,
        name: detail.name || detail.task_id,
        session_id: detail.session_id || null,
        pending_overrides: detail.pending_overrides || [],
      });
    }
    if (window.showResearchGateCard) {
      window.showResearchGateCard({
        task_id: detail.task_id,
        name: detail.name || detail.task_id,
        session_id: detail.session_id || null,
        pending_overrides: detail.pending_overrides || [],
      });
    }
  }

  // Discrete stage nodes: done ✓ / current ● / pending ○, chained with arrows. State is derived
  // purely from the canonical array order (index < current → done, index == current → current,
  // else pending) — never a per-stage special case. Each pill rides with its trailing arrow in one
  // flex "step" unit, so a narrow sidebar wraps the chain at pill boundaries and a wrapped row
  // always starts with a stage, never an orphan ➔.
  function renderStageNodes(stage) {
    const box = section("Stage");
    const bar = el("div", "research-stage-bar");
    const curIdx = STAGES.indexOf(stage);
    STAGES.forEach((s, i) => {
      const state = i === curIdx ? "current" : (curIdx > i ? "done" : "pending");
      const step = el("span", "research-stage-step");
      const pill = el("span", `research-stage-pill ${state}`);
      pill.appendChild(el("span", "research-stage-mark", state === "done" ? "✓" : (state === "current" ? "●" : "○")));
      pill.appendChild(el("span", "research-stage-label", s));
      step.appendChild(pill);
      if (i < STAGES.length - 1) step.appendChild(el("span", "research-stage-arrow", "➔"));
      bar.appendChild(step);
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
      // PASS / OVERRIDE clear the gate (green); FAIL shows red so a recorded diagnostic reads at
      // a glance; anything else (NOT_RUN / …) stays a neutral pending chip.
      const tone = status === "PASS" || status === "OVERRIDE" ? "pass" : status === "FAIL" ? "fail" : "pending";
      const chip = el("span", `research-gate-chip ${tone}`, `${GATE_NAMES[name]}: ${status}`);
      wrap.appendChild(chip);
    }
    box.appendChild(wrap);
    return box;
  }

  // ── evidence-graph summary (status column) + detail drawer (right) ────────

  // Aggregate counts over the three evidence kinds. Returns null when the graph has none of
  // them, so the status column shows no evidence section before the first record.
  function graphCounts(nodes) {
    const c = { sources: 0, verified: 0, claims: 0, evidence: 0, supports: 0, contradicts: 0, neutral: 0 };
    const list = (t) => (nodes && nodes[t]) || [];
    for (const n of list("Source")) {
      c.sources++;
      if (n.verification_status === "verified") c.verified++;
    }
    c.claims = list("Claim").length;
    for (const n of list("Evidence")) {
      c.evidence++;
      if (n.verdict === "supports") c.supports++;
      else if (n.verdict === "contradicts") c.contradicts++;
      else if (n.verdict === "neutral") c.neutral++;
    }
    return c.sources + c.claims + c.evidence === 0 ? null : c;
  }

  // Cheap content signature of the evidence graph (type × id × verdict/verification), used to
  // refresh an open drawer in place as a run records more nodes, and to notice a wipe (empty
  // string) so an edition reset never leaves the previous edition's graph on screen.
  function graphSig(detail) {
    const parts = [];
    const nodes = detail && detail.nodes;
    if (nodes) {
      for (const type of ["Source", "Claim", "Evidence"]) {
        for (const n of nodes[type] || []) {
          parts.push(`${type}:${n.id}:${n.verification_status || n.verdict || ""}`);
        }
      }
    }
    parts.sort();
    return parts.join("|");
  }

  // Compact summary for the narrow status column (replaces the old flat node-chip dump, which
  // wrapped long Claim/Source labels across the whole sidebar). Long text lives in the drawer.
  function renderEvidenceSummary(detail) {
    const c = graphCounts(detail && detail.nodes);
    if (!c) return null;
    const box = section("Evidence graph");
    const rows = el("div", "evg");
    rows.appendChild(evgRow("Sources", c.sources, c.verified ? [[`${c.verified} verified`, "ok"]] : []));
    rows.appendChild(evgRow("Claims", c.claims, []));
    const evParts = [];
    if (c.supports) evParts.push([`${c.supports} supports`, "ok"]);
    if (c.contradicts) evParts.push([`${c.contradicts} contradicts`, "bad"]);
    if (c.neutral) evParts.push([`${c.neutral} neutral`, "dim"]);
    rows.appendChild(evgRow("Evidence", c.evidence, evParts));
    const btn = el("button", "ghost evg-btn", "View graph →");
    btn.title = "Open the full Source · Evidence · Claim detail";
    btn.addEventListener("click", () => openGraphDrawer(detail));
    rows.appendChild(btn);
    box.appendChild(rows);
    return box;
  }

  // One key/value line of the summary card. extra holds [label, tone] chips shown beside the
  // count; tones are "ok" (verified/supports), "bad" (contradicts) or "dim" (neutral).
  function evgRow(key, value, extra) {
    const row = el("div", "evg-row");
    row.appendChild(el("span", "evg-key", key));
    row.appendChild(el("span", "evg-val", String(value)));
    if (extra && extra.length) {
      const chips = el("span", "evg-extra");
      for (const [label, tone] of extra) chips.appendChild(el("span", `evg-chip ${tone}`, label));
      row.appendChild(chips);
    }
    return row;
  }

  // Drawer open/close. Show/hide is pure CSS (visibility + transform transition on .open), so no
  // element is ever display:none while animating; pointer events are inert while hidden.
  function bindGraphDrawer() {
    const root = document.getElementById("graph-drawer");
    if (!root || graphDrawerBound) return;
    graphDrawerBound = true;
    const scrim = root.querySelector(".graph-drawer-scrim");
    const closeBtn = root.querySelector(".graph-drawer-close");
    if (scrim) scrim.addEventListener("click", closeGraphDrawer);
    if (closeBtn) closeBtn.addEventListener("click", closeGraphDrawer);
    // Esc closes the drawer only while it is actually open; other overlays own their own keys.
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && graphDrawerOpen) closeGraphDrawer();
    });
  }

  function openGraphDrawer(detail) {
    const root = document.getElementById("graph-drawer");
    if (!root) return;
    bindGraphDrawer();
    graphDrawerTaskId = detail && detail.task_id;
    graphDrawerSig = graphSig(detail);
    graphDrawerOpen = true;
    const title = root.querySelector(".graph-drawer-title");
    if (title) title.textContent = `${detail && detail.name ? `${detail.name} — ` : ""}Evidence graph`;
    renderGraphDrawerBody(detail);
    root.setAttribute("aria-hidden", "false");
    // Add .open a frame later so the browser registers the (re)shown layout before transitioning.
    requestAnimationFrame(() => { if (graphDrawerOpen) root.classList.add("open"); });
  }

  function closeGraphDrawer() {
    graphDrawerOpen = false;
    graphDrawerTaskId = null;
    graphDrawerSig = null;
    const root = document.getElementById("graph-drawer");
    if (!root) return;
    root.classList.remove("open");
    root.setAttribute("aria-hidden", "true");
  }

  // Keep an open drawer honest against a freshly re-rendered detail: same task + growing graph →
  // refresh the body in place; task switch or an emptied graph (edition reset) → close. This is
  // the "status to zero" guarantee — no stale prior-edition graph survives a render boundary.
  function syncGraphDrawer(detail) {
    if (!graphDrawerOpen) return;
    if (graphDrawerTaskId !== detail.task_id) return closeGraphDrawer();
    const sig = graphSig(detail);
    if (!sig) return closeGraphDrawer();
    if (sig !== graphDrawerSig) {
      graphDrawerSig = sig;
      renderGraphDrawerBody(detail);
    }
  }

  // Flat list of Source/Claim/Evidence nodes + id→node map, sourced from either the grouped
  // detail.nodes or the raw detail.graph.nodes (whichever the payload carried).
  function evidenceGraphNodes(detail) {
    const byId = new Map();
    const list = [];
    const add = (n) => { if (n && n.id && !byId.has(n.id)) { byId.set(n.id, n); list.push(n); } };
    const grouped = detail && detail.nodes;
    for (const type of ["Source", "Claim", "Evidence"]) {
      for (const n of (grouped && grouped[type]) || []) add(n);
    }
    const flat = detail && detail.graph && detail.graph.nodes;
    if (Array.isArray(flat)) for (const n of flat) add(n);
    return { byId, list };
  }

  // Full Source–Evidence–Claim mapping for the right-hand drawer: grouped Claims with their
  // verdict-carrying Evidence inline (excerpt, facts, source URL), a Sources index, and any
  // unlinked Evidence (e.g. neutral) at the end so nothing is dropped.
  function renderGraphDrawerBody(detail) {
    const root = document.getElementById("graph-drawer");
    if (!root) return;
    const body = root.querySelector(".graph-drawer-body");
    if (!body) return;
    body.innerHTML = "";

    const { byId, list } = evidenceGraphNodes(detail);
    const kinds = { Source: [], Claim: [], Evidence: [] };
    for (const n of list) if (kinds[n.type]) kinds[n.type].push(n);

    // Index the graph edges: claim→evidence (kind carries the verdict) and evidence→source
    // (depends_on). Fallbacks cover payloads whose edges were omitted.
    const evOfClaim = new Map(); // claim id -> evidence ids
    const srcOfEv = new Map();   // evidence id -> source node
    const edges = (detail && detail.graph && detail.graph.edges) || [];
    for (const e of edges) {
      if (!e || !e.src || !e.dst) continue;
      const from = byId.get(e.src);
      const to = byId.get(e.dst);
      if (!from || !to) continue;
      if (from.type === "Claim" && to.type === "Evidence") {
        if (!evOfClaim.has(from.id)) evOfClaim.set(from.id, []);
        evOfClaim.get(from.id).push(to.id);
      } else if (from.type === "Evidence" && to.type === "Source") {
        srcOfEv.set(from.id, to);
      }
    }
    const sourceFor = (ev) => {
      if (srcOfEv.has(ev.id)) return srcOfEv.get(ev.id);
      if (ev.source_url) return kinds.Source.find(
        (s) => s.url === ev.source_url || s.canonical_url === ev.source_url,
      ) || null;
      return null;
    };

    // One evidence card: verdict badge + label, the source it came from, excerpt + facts.
    const evidenceNode = (ev) => {
      const verdict = ev.verdict || "unknown";
      const tone = verdict === "supports" ? "ok" : verdict === "contradicts" ? "bad" : "dim";
      const card = el("div", `evg-card ${tone}`);
      const top = el("div", "evg-card-top");
      top.appendChild(el("span", `evg-badge ${tone}`, verdict));
      const label = ev.label || ev.id || "Evidence";
      const lab = el("div", "evg-card-title", clipText(label, 180));
      if (String(label).length > 180) lab.title = label;
      top.appendChild(lab);
      card.appendChild(top);
      const src = sourceFor(ev);
      if (src) {
        const row = el("div", "evg-src");
        row.appendChild(el("span", "evg-src-arrow", "from"));
        row.appendChild(el("span", "evg-url", clipText(src.url || src.canonical_url || src.label || src.id, 160)));
        card.appendChild(row);
      }
      if (ev.excerpt) {
        const t = String(ev.excerpt);
        const ex = el("div", "evg-excerpt", clipText(t, 600));
        if (t.length > 600) ex.title = t;
        card.appendChild(ex);
      }
      if (Array.isArray(ev.facts) && ev.facts.length) {
        const ul = el("ul", "evg-facts");
        for (const f of ev.facts) ul.appendChild(el("li", "", clipText(String(f), 240)));
        card.appendChild(ul);
      }
      return card;
    };

    // Header strip of the current counts (mirrors the status-column card).
    const c = graphCounts(detail && detail.nodes) || {};
    const strip = el("div", "evg-strip");
    const stripChip = (label, tone) => el("span", `evg-strip-chip${tone ? ` ${tone}` : ""}`, label);
    strip.appendChild(stripChip(`Sources ${c.sources || 0}`));
    if (c.verified) strip.appendChild(stripChip(`${c.verified} verified`, "ok"));
    strip.appendChild(stripChip(`Claims ${c.claims || 0}`));
    strip.appendChild(stripChip(`Evidence ${c.evidence || 0}`));
    if (c.supports) strip.appendChild(stripChip(`${c.supports} supports`, "ok"));
    if (c.contradicts) strip.appendChild(stripChip(`${c.contradicts} contradicts`, "bad"));
    if (c.neutral) strip.appendChild(stripChip(`${c.neutral} neutral`, "dim"));
    body.appendChild(strip);

    // Claims → evidence mapping tree (the primary view).
    const claimsSec = el("div", "evg-sec");
    claimsSec.appendChild(el("div", "evg-sec-title", `Claims · ${kinds.Claim.length}`));
    if (!kinds.Claim.length) {
      claimsSec.appendChild(el("div", "evg-muted", "No claims recorded yet."));
    } else {
      for (const cl of kinds.Claim) {
        const blk = el("div", "evg-claim");
        const head = el("div", "evg-claim-head");
        head.appendChild(el("div", "evg-claim-label", cl.label || cl.id || "Claim"));
        if (cl.id) head.appendChild(el("span", "evg-id", clipText(cl.id, 48)));
        blk.appendChild(head);
        const evIds = evOfClaim.get(cl.id) || [];
        const evs = evIds.map((id) => byId.get(id)).filter(Boolean);
        if (evs.length) {
          for (const ev of evs) blk.appendChild(evidenceNode(ev));
        } else {
          blk.appendChild(el("div", "evg-muted", "No supporting or contradicting evidence yet."));
        }
        claimsSec.appendChild(blk);
      }
    }
    body.appendChild(claimsSec);

    // Sources index (all fetched sources, verified or not).
    const srcSec = el("div", "evg-sec");
    srcSec.appendChild(el(
      "div", "evg-sec-title",
      `Sources · ${kinds.Source.length}${c.verified ? ` · ${c.verified} verified` : ""}`,
    ));
    if (!kinds.Source.length) {
      srcSec.appendChild(el("div", "evg-muted", "No sources fetched yet."));
    } else {
      for (const s of kinds.Source) {
        const row = el("div", "evg-src-row");
        row.appendChild(
          s.verification_status === "verified"
            ? el("span", "evg-badge ok", "verified")
            : el("span", "evg-badge dim", "unverified"),
        );
        const url = s.url || s.canonical_url || s.label || s.id || "?";
        row.appendChild(el("span", "evg-url", String(url)));
        const metaBits = [];
        if (s.content_status) metaBits.push(s.content_status);
        if (s.full_char_len != null) metaBits.push(`${s.full_char_len} ch`);
        if (s.asset_id) metaBits.push(clipText(s.asset_id, 24));
        if (metaBits.length) row.appendChild(el("span", "evg-src-meta", metaBits.join(" · ")));
        srcSec.appendChild(row);
      }
    }
    body.appendChild(srcSec);

    // Evidence with no claim link (e.g. a neutral re-annotation) — shown so nothing is hidden.
    const placed = new Set();
    for (const ids of evOfClaim.values()) for (const id of ids) placed.add(id);
    const orphans = kinds.Evidence.filter((ev) => !placed.has(ev.id));
    if (orphans.length) {
      const oSec = el("div", "evg-sec");
      oSec.appendChild(el("div", "evg-sec-title", `Unlinked evidence · ${orphans.length}`));
      for (const ev of orphans) oSec.appendChild(evidenceNode(ev));
      body.appendChild(oSec);
    }
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
    // A run in progress supersedes the previous terminal outcome: while ``is_running`` the old
    // banner must not keep claiming the task is Paused/stopped (``last_block`` is only cleared
    // when the *next* run reaches its own terminal state, so the previous one lingers).
    if (detail.is_running) return null;
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
    // Read-only echo of the task's locked execution mode, sat next to Run so it is never
    // mistaken for an editable choice: the mode was set once at creation and cannot be changed.
    const mode = modeInfo(detail.execution_mode);
    const modeChip = el(
      "span", `rtv-chip mode${detail.execution_mode === "progressive" ? " progressive" : ""}`,
      mode.label
    );
    modeChip.title = mode.tip;
    actions.appendChild(modeChip);
    // Run starts (or resumes) the task's run in one click; Stop requests the cooperative cancel
    // of the in-flight run; Delete cascades the cloud folder + state. Run and Delete disable
    // while THIS task's run is in flight (isTaskRunning(selectedTask)), Run switches to its
    // running style, and Stop is its mirror — only actionable while the run is live.
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
  // .rtv-kids containers whose dotted border-left is the per-level indent guide. The three
  // canonical subfolders (materials/, outputs/, temp/) always render — even empty — so a fresh
  // task shows its stable layout; per-run folders (temp/v1 …) and scrape/ nest beneath temp/.
  // Root mirrors (task_spec.json / session_history.json) read as the task folder's own files.
  // Folder rows start collapsed but the task row is expanded one level; ▸/▾ toggles expand in
  // place and each file row opens in the preview column to the right. The expansion set is kept
  // per task across live re-renders.
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
    const filesByDir = new Map();
    for (const f of detail.cloud_files || []) {
      let sub = f.folder_path || "";
      if (sub.startsWith(cloudPath + "/")) sub = sub.slice(cloudPath.length + 1);
      else if (sub !== cloudPath) sub = sub.replace(/^\/+/, "");
      else sub = "";
      if (!filesByDir.has(sub)) filesByDir.set(sub, []);
      filesByDir.get(sub).push(f);
    }
    // Root-level mirrors (task_spec.json / session_history.json) are rendered as the task
    // folder's own files below, so no separate ``roots`` list is needed.

    // Ordered hierarchical folder set. The three canonical task folders (materials/, outputs/,
    // temp/) always exist — even when empty — so a brand-new task shows the full stable layout
    // instead of hiding temp/ until the first run writes into it. File-bearing folders then add
    // their deeper segments (temp/v1, temp/v1/scrape) beneath the matching parent.
    const dirSet = new Set(["materials", "outputs", "temp"]);
    for (const dir of filesByDir.keys()) {
      if (!dir) continue;
      const parts = dir.split("/");
      for (let i = 1; i <= parts.length; i++) dirSet.add(parts.slice(0, i).join("/"));
    }
    // Immediate subfolders of ``dir`` (dirs exactly one path segment deeper).
    const childDirs = (dir) => {
      const prefix = dir ? `${dir}/` : "";
      return [...dirSet]
        .filter((d) => d !== dir && d.startsWith(prefix) && !d.slice(prefix.length).includes("/"))
        .sort((a, b) => a.localeCompare(b));
    };
    const open = treeOpenState.get(detail.task_id) || new Set([""]);
    treeOpenState.set(detail.task_id, open);

    function toggle(dir) {
      if (open.has(dir)) open.delete(dir);
      else open.add(dir);
      const treeEl = wrap.querySelector(".rtv-tree");
      if (treeEl) treeEl.replaceWith(buildTree());
    }

    // Subtree file totals: how many files a folder holds including everything under it. Direct
    // counts alone mislead for folders that only nest other folders — temp/ stores runs as
    // temp/v1/, temp/v2/, … temp/vN/scrape/, so its own file count is always 0 and a parent
    // row must sum its descendants or it reads "(0)" the moment a run writes files beneath it.
    const fileCountBelow = new Map();
    for (const [dir, list] of filesByDir) {
      if (!dir) continue; // the task root row is tallied below
      fileCountBelow.set(dir, (fileCountBelow.get(dir) || 0) + list.length);
      const parts = dir.split("/");
      let acc = "";
      for (const seg of parts) {
        acc = acc ? `${acc}/${seg}` : seg;
        fileCountBelow.set(acc, (fileCountBelow.get(acc) || 0) + list.length);
      }
    }
    let totalFiles = 0;
    for (const list of filesByDir.values()) totalFiles += list.length;
    fileCountBelow.set("", totalFiles);

    // Right-click export → shared cloud export (clouddrive.js). A file row exports that single
    // file (Save dialog); a folder row downloads its whole subtree into a picked folder with the
    // on-screen structure preserved (each relPath is prefixed with the clicked folder's name).
    function folderExportEntries(dir) {
      const topName = dir === ""
        ? (cloudPath.split("/").filter(Boolean).pop() || "task folder")
        : dir.slice(dir.lastIndexOf("/") + 1);
      const prefix = dir ? dir + "/" : "";
      const out = [];
      for (const [d, list] of filesByDir) {
        if (!d) continue;
        if (dir && d !== dir && !d.startsWith(prefix)) continue;
        const under = dir ? (d === dir ? "" : d.slice(prefix.length)) : d;
        for (const f of list) out.push({ assetId: f.id, name: f.name, relPath: [topName, under, f.name].filter(Boolean).join("/") });
      }
      if (!dir) {
        for (const f of (filesByDir.get("") || [])) out.push({ assetId: f.id, name: f.name, relPath: `${topName}/${f.name}` });
      }
      return out.sort((a, b) => a.relPath.localeCompare(b.relPath));
    }

    function showTreeRowMenu(e, f, dir) {
      e.preventDefault();
      e.stopPropagation();
      if (!window.cloudCtxMenu) return;
      const folderName = dir === ""
        ? (cloudPath.split("/").filter(Boolean).pop() || "task folder")
        : dir.slice(dir.lastIndexOf("/") + 1);
      const items = [];
      if (f) {
        items.push({ label: "📤 Export file…", fn: () => { if (window.exportCloudFile) window.exportCloudFile(f); } });
      } else {
        items.push({ label: "📤 Export folder…", fn: () => { if (window.exportCloudFolder) window.exportCloudFolder(folderExportEntries(dir), folderName); } });
      }
      window.cloudCtxMenu(e.clientX, e.clientY, items);    }

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
      row.addEventListener("contextmenu", (e) => showTreeRowMenu(e, f, null));
      return row;
    }

    // A folder row: ▸/▾ toggles its children. The count (n) hangs right after the name and,
    // for empty folders, a grey "(empty)" tag — all on the same line (no separate empty row).
    // The folder's children are its direct subfolders (nested tree) plus the files in it.
    function folderRow(dir) {
      const files = filesByDir.get(dir) || [];
      const kids = childDirs(dir);
      const hasKids = files.length > 0 || kids.length > 0;
      const isOpen = open.has(dir);
      const label = dir === "" ? (cloudPath || "Task folder") : dir.slice(dir.lastIndexOf("/") + 1);
      const row = el("div", "cd-row cd-folder rtv-folder-row");
      const tw = el("span", "cd-tw", hasKids ? (isOpen ? "▾" : "▸") : "");
      row.appendChild(tw);
      row.appendChild(el("span", "cd-icon", dir === "" ? "🗂" : "📁"));
      row.appendChild(el("span", "cd-name", label));
      row.appendChild(el("span", "rtv-count", `(${fileCountBelow.get(dir) || 0})`));
      if (!files.length && !kids.length) row.appendChild(el("span", "rtv-empty-tag", "(empty)"));
      row.title = dir === "" ? cloudPath : dir;
      if (hasKids) {
        tw.addEventListener("click", (e) => { e.stopPropagation(); toggle(dir); });
        row.addEventListener("click", () => toggle(dir));
      }
      row.addEventListener("contextmenu", (e) => showTreeRowMenu(e, null, dir));
      return row;
    }

    // Recursive children of one folder: each immediate subfolder (recursing into it when it is
    // open), then the files sitting directly in it. The root mirrors (task_spec.json /
    // session_history.json) are simply the task root folder's own files.
    function childrenOf(dir) {
      const kids = el("div", "rtv-kids");
      for (const child of childDirs(dir)) {
        kids.appendChild(folderRow(child));
        if (open.has(child)) kids.appendChild(childrenOf(child));
      }
      const files = filesByDir.get(dir) || [];
      if (files.length) {
        const sub = el("div", "rtv-kids");
        for (const f of files.slice(0, 50)) sub.appendChild(fileRow(f));
        kids.appendChild(sub);
      }
      return kids;
    }

    function buildTree() {
      const treeEl = el("div", "rtv-tree");
      treeEl.appendChild(folderRow(""));
      if (open.has("")) treeEl.appendChild(childrenOf(""));
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
    const hint = el("span", "rtv-col-hint", "click a file to preview");
    head.appendChild(hint);
    // ⤢ lifts the preview column into a window-covering overlay (Esc exits) — the split column
    // is often too narrow to read a long report. Shown only while a file is open, like the ×.
    // It sits to the LEFT of the close button so the rightmost control is always "×".
    const fsBtn = el("button", "ghost rtv-preview-fs", "⤢");
    fsBtn.type = "button";
    fsBtn.title = "Fullscreen (Esc to exit)";
    fsBtn.classList.add("hidden");
    fsBtn.addEventListener("click", () => setPreviewFs(col, !col.classList.contains("rtv-preview-fs-on")));
    head.appendChild(fsBtn);
    const closeBtn = el("button", "ghost rtv-preview-close", "×");
    closeBtn.type = "button";
    closeBtn.title = "Close the open file preview";
    closeBtn.classList.add("hidden");
    closeBtn.addEventListener("click", closePreview);
    head.appendChild(closeBtn);
    col.appendChild(head);
    const body = el("div", "rtv-preview-body");
    body.appendChild(
      el("div", "rtv-preview-placeholder",
        "Select a file in the Working directory to preview its contents here.")
    );
    col.appendChild(body);
    col._title = head.querySelector(".rtv-col-title");
    col._hint = hint;
    col._closeBtn = closeBtn;
    col._fsBtn = fsBtn;
    col._body = body;
    return col;
  }

  // Enter / exit preview fullscreen. The flag is kept on the column node so a live re-render
  // (which re-attaches the same node) preserves the state; task switches build a fresh column
  // and start back in the split layout. Esc is bound once at module level.
  function setPreviewFs(col, on) {
    col.classList.toggle("rtv-preview-fs-on", on);
    if (col._fsBtn) {
      col._fsBtn.textContent = on ? "⤡" : "⤢";
      col._fsBtn.title = on ? "Exit fullscreen (Esc)" : "Fullscreen (Esc to exit)";
    }
    if (on && !previewFsEscBound) {
      previewFsEscBound = true;
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && previewColEl && previewColEl.classList.contains("rtv-preview-fs-on")) {
          setPreviewFs(previewColEl, false);
        }
      });
    }
  }

  // Close the currently open file: clear the selection back to the empty placeholder state. The
  // preview column itself stays (it is the workbench's right-hand panel); only the open file is
  // dismissed.
  function closePreview() {
    if (!previewColEl) return;
    previewState = { taskId: previewColEl._taskId, file: null, size: null };
    previewRefreshedAt = 0;
    if (previewTitleEl) previewTitleEl.textContent = "File preview";
    if (previewColEl._hint) previewColEl._hint.textContent = "click a file to preview";
    if (previewColEl._closeBtn) previewColEl._closeBtn.classList.add("hidden");
    if (previewColEl._fsBtn) {
      previewColEl._fsBtn.classList.add("hidden");
      setPreviewFs(previewColEl, false); // closing the file always drops back to the split view
    }
    previewPlaceholder("Select a file in the Working directory to preview its contents here.");
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
    // A file is now open: drop the hint and surface the fullscreen + close (×) controls in the header.
    if (previewColEl._hint) previewColEl._hint.textContent = "";
    if (previewColEl._closeBtn) previewColEl._closeBtn.classList.remove("hidden");
    if (previewColEl._fsBtn) previewColEl._fsBtn.classList.remove("hidden");
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
      // The open file vanished from the task folder (agent cleanup / edition reset) — close the
      // preview and reset the header chrome (title, hint, × button).
      closePreview();
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

  // ── live revision monitor (short-poll) ───────────────────────────────────
  // The selected task is watched by periodically GET-ing its detail endpoint — each request
  // completes in milliseconds and releases its socket right away, so no connection is ever
  // pinned to a task (the old per-task SSE stream leaked upstream sockets on task switches and
  // saturated Chromium's 6-per-origin pool after ~6 clicks). Each tick compares the
  // authoritative project_revision against the last rendered floor; a strictly-newer revision
  // schedules one coalesced refetch. The interval adapts to run state: fast while RUNNING,
  // slow when idle, and it stops entirely when no task is selected (stopMonitor).
  const POLL_RUNNING_MS = 1500;
  const POLL_IDLE_MS = 5000;

  function startMonitor(taskId) {
    stopMonitor();
    monitorTaskId = taskId;
    if (!bearerToken()) return; // guests have no tasks — nothing to watch
    monitorWasRunning = isTaskRunning(taskId);
    monitorPollTimer = setTimeout(pollMonitor, 0);
  }

  function scheduleNextPoll() {
    if (!monitorTaskId) return;
    if (monitorPollTimer) clearTimeout(monitorPollTimer);
    monitorPollTimer = setTimeout(pollMonitor,
      isTaskRunning(monitorTaskId) ? POLL_RUNNING_MS : POLL_IDLE_MS);
  }

  async function pollMonitor() {
    monitorPollTimer = null;
    const taskId = monitorTaskId;
    if (!taskId) return;
    const token = bearerToken();
    if (!token) return;
    try {
      const res = await fetch(`/api/research/tasks/${encodeURIComponent(taskId)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 401 || res.status === 404) return; // auth/task gone — stop watching
      if (res.ok) {
        const d = await res.json();
        if (monitorTaskId !== taskId) return; // the user switched tasks during the request
        const wasRunning = monitorWasRunning;
        monitorWasRunning = !!d.is_running;
        if (wasRunning && !d.is_running) {
          activityNotice(taskId, "⏹ Run ended — status refreshed.");
        }
        const rev = Number(d.project_revision || 0);
        if (rev > monitorRevision) {
          monitorRevision = rev; // floor moves now; the refetch below is idempotent on it
          scheduleRefresh(taskId);
        }
      }
    } catch { /* transient network/backend error — next tick retries */ }
    scheduleNextPoll();
  }

  function stopMonitor() {
    monitorTaskId = null;
    refreshQueued = false;
    if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
    if (monitorPollTimer) { clearTimeout(monitorPollTimer); monitorPollTimer = null; }
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
    await renderStatusDetail(detail, { openSession: false });
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
