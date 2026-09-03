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

  let selectedTask = null; // { task_id, name }
  // Research run state: a run is in flight when the desktop auto-started it (Run) or the user
  // activated it by typing in the task's session (sendChat attaches the research handoff).
  // While running, the Run + Delete Task controls stay disabled and the Run button shows its
  // running style; the Activity feed below mirrors the live run events.
  let researchRunning = false;
  let runningTaskId = null;      // task the in-flight run belongs to (blocks deleting it)
  let activityEl = null;         // live Activity section (survives same-task pane re-renders)
  let activityForTask = null;    // task_id the current activity feed belongs to

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
      btn.disabled = researchRunning;
      if (btn.classList.contains("run-start")) {
        btn.textContent = runStartText();
        btn.classList.toggle("running", researchRunning);
      }
    });
  }

  // Research runs that are already RUNNING on the server (adopted when a task view renders).
  function adoptRunState(detail) {
    if (detail && detail.is_running) {
      researchRunning = true;
      runningTaskId = detail.task_id;
    }
  }

  window.researchRunActive = (taskId, on) => {
    researchRunning = !!(taskId && on);
    if (on && taskId) runningTaskId = taskId;
    syncRunCtl();
    if (activityEl) {
      activityEl.classList.toggle("running", researchRunning);
      const meta = activityEl._meta;
      if (researchRunning && meta && meta.dataset.hold !== "true") meta.textContent = "Running…";
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
    const box = el("div", "research-section rtv-activity");
    const head = el("div", "rtv-activity-head");
    head.appendChild(el("span", "rtv-activity-dot"));
    head.appendChild(el("span", "rtv-activity-title", "Activity"));
    const meta = el("span", "rtv-activity-meta", "Idle");
    meta.dataset.hold = "false";
    head.appendChild(meta);
    box.appendChild(head);
    const body = el("div", "rtv-activity-body");
    body.appendChild(
      el("div", "rtv-activity-empty",
        "No run in progress — press ▶ Run or send a message in the task's session to start.")
    );
    box.appendChild(body);
    box._body = body;
    box._meta = meta;
    return box;
  }

  function ensureActivity(taskId) {
    if (!activityEl || activityForTask !== taskId) {
      activityEl = buildActivity();
      activityForTask = taskId;
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
    const body = activityEl._body;
    if (!body) return;
    const empty = body.querySelector(".rtv-activity-empty");
    if (empty) empty.remove();
    const row = el("div", "rtv-activity-line");
    if (evt.type === "notice") { row.classList.add("info"); row.textContent = clipText(evt.data, 220); }
    else if (evt.type === "thinking") { row.classList.add("think"); row.textContent = `💭 ${clipText(evt.data, 180)}`; }
    else if (evt.type === "tool") { row.classList.add("tool"); row.textContent = `⚙️ ${(evt.data && evt.data.name) || "tool"}`; }
    else if (evt.type === "step-answer") { row.classList.add("boundary"); row.textContent = "—"; }
    else if (evt.type === "done") { row.classList.add("ok"); row.textContent = "✓ Run turn finished."; }
    else return;
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
    adoptRunState(detail);
    recordResearchSession(detail);
    // Open this task's dedicated session in the chat — silent, no message sent. This is what
    // makes task selection side-effect free (app.js openResearchSession); a repeated open of
    // the same session is a no-op, so it never forks a new one.
    if (window.openResearchSession) {
      window.openResearchSession(detail.task_id, detail.name || detail.task_id, detail.session_id || null);
    }
    statusBody.innerHTML = "";
    const head = el("div", "research-status-head");
    head.appendChild(el("div", "research-status-name", detail.name || detail.task_id));
    const sub = el("div", "research-status-sub", `${detail.status}${detail.is_running ? " — running" : ""}${detail.description ? ` — ${detail.description}` : ""}`);
    head.appendChild(sub);
    statusBody.appendChild(head);
    statusBody.appendChild(renderStageNodes(detail.stage));
    const gates = renderGates(detail.gates || {});
    if (gates) statusBody.appendChild(gates);
    const nodes = renderNodes(detail.nodes || {});
    if (nodes) statusBody.appendChild(nodes);
    statusBody.appendChild(renderArtifacts(detail));
    const sess = renderSession(detail);
    if (sess) statusBody.appendChild(sess);
    statusBody.appendChild(renderInventory(detail));
    statusBody.appendChild(renderResume(detail));
    syncRunCtl(); // reflect any run state on the freshly rendered Run control
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

  function renderArtifacts(detail) {
    const box = section("Artifacts");
    const list = el("div", "research-artifacts-list");
    const artifacts = detail.artifacts || [];
    if (!artifacts.length) {
      list.appendChild(el("div", "research-empty", "No artifacts yet."));
      box.appendChild(list);
      return box;
    }
    for (const a of artifacts) {
      const row = el("div", "research-artifact-row");
      const main = el("div", "research-row-main");
      main.appendChild(el("div", "research-row-title", `${a.artifact_id} · v${a.version}`));
      main.appendChild(el("div", "research-row-sub", `Status ${a.status}${a.drive_path ? ` · ${a.drive_path}` : ""}`));
      row.appendChild(main);
      const view = el("button", "research-mini-btn", "View");
      view.addEventListener("click", () => viewArtifact(detail.task_id, a));
      row.appendChild(view);
      list.appendChild(row);
    }
    box.appendChild(list);
    return box;
  }

  // The bound research session's transcript — the chat that drives this task. Rendered as a
  // compact read-only conversation so the monitor shows what the agent has been told / said.
  function renderSession(detail) {
    const sess = detail.session || {};
    const turns = sess.turns || [];
    if (!turns.length) return null;
    const box = section("Session");
    const sub = el("div", "research-status-sub");
    sub.textContent = sess.session_id ? `chat ${sess.session_id.slice(0, 8)}…` : "";
    box.appendChild(sub);
    const list = el("div", "research-session");
    for (const t of turns.slice(-20)) {
      const row = el("div", `research-session-turn ${t.role === "assistant" ? "assistant" : "user"}`);
      row.appendChild(el("div", "research-session-role", t.role === "assistant" ? "Assistant" : "You"));
      const body = el("div", "research-session-body");
      body.innerHTML = renderMarkdown(t.content);
      row.appendChild(body);
      list.appendChild(row);
    }
    if (turns.length > 20) {
      list.appendChild(el("div", "research-empty", `… ${turns.length - 20} earlier turns`));
    }
    box.appendChild(list);
    return box;
  }

  // Read-only artifact view (inline in the status pane, with a back button).
  async function viewArtifact(taskId, a) {
    if (!statusBody) return;
    statusBody.innerHTML = "";
    statusBody.appendChild(el("div", "research-status-empty", "Loading…"));
    let res;
    try {
      res = await apiFetch(
        `/research/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(a.artifact_id)}?version=${a.version}`
      );
    } catch (e) {
      statusBody.innerHTML = "";
      statusBody.appendChild(el("div", "research-status-empty", `View failed: ${e.message}`));
      return;
    }
    statusBody.innerHTML = "";
    const head = el("div", "research-status-head");
    const back = el("button", "research-mini-btn", "← Back to status");
    back.addEventListener("click", () => loadStatus(taskId));
    head.appendChild(back);
    head.appendChild(el("div", "research-status-name", `${a.artifact_id} v${a.version}`));
    statusBody.appendChild(head);
    const content = el("div", "research-artifact-content");
    content.innerHTML = renderMarkdown(res.content);
    statusBody.appendChild(content);
  }

  // Working-directory projection: every file inside the task's cloud folder (task_spec.json /
  // session_history.json at the root, materials/, outputs/), grouped by subfolder. The two
  // work folders are always shown — even empty — so the layout is visible from the start.
  function renderInventory(detail) {
    const box = section("Working directory");
    const wrap = el("div", "research-inventory");
    const cloudPath = detail.cloud_folder_path || "";
    const groups = new Map();
    for (const f of detail.cloud_files || []) {
      let sub = f.folder_path || "";
      if (sub.startsWith(cloudPath + "/")) sub = sub.slice(cloudPath.length + 1);
      else sub = sub === cloudPath ? "" : sub;
      if (!groups.has(sub)) groups.set(sub, []);
      groups.get(sub).push(f);
    }
    const extra = Array.from(groups.keys()).filter((k) => k && k !== "materials" && k !== "outputs");
    for (const seg of ["", "materials", "outputs", ...extra]) {
      const list = groups.get(seg) || [];
      const item = el("div", "research-inventory-item");
      const label = seg === "" ? (cloudPath || "Task folder") : `${seg}/`;
      item.appendChild(el("div", "research-inventory-label", `📁 ${label} (${list.length})`));
      if (!list.length) {
        item.appendChild(el("div", "research-empty", "(empty)"));
      } else {
        for (const f of list.slice(0, 20)) {
          const file = el("div", "research-inventory-file", `${fileTypeIcon(f)} ${f.name} · ${formatBytes(f.size)}`);
          if (f.rag_status === "INDEXED") file.title = "In Knowledge Base";
          item.appendChild(file);
        }
      }
      wrap.appendChild(item);
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

  function renderResume(detail) {
    if (!window.openResearchSession || !window.startResearchRun) return el("div");
    const row = el("div", "research-status-resume");
    // Run starts (or resumes) the task's run: opens its dedicated session, then auto-sends the
    // run message through the research handoff. While a run is in flight every Run / Delete
    // control is disabled and the Run button shows its running style.
    const btn = el("button", "run-ctl run-start", runStartText());
    btn.addEventListener("click", () =>
      window.startResearchRun(detail.task_id, detail.name || detail.task_id, detail.session_id)
    );
    row.appendChild(btn);
    return row;
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
    adoptRunState(detail);
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
    // Run starts (or resumes) the task's run in one click; Delete cascades the cloud folder +
    // state. Both are disabled while a run is in flight (researchRunning), and Run switches to
    // its running style so the in-flight state is visible everywhere at once.
    const openBtn = el("button", "run-ctl run-start", runStartText());
    openBtn.addEventListener("click", () =>
      window.startResearchRun(detail.task_id, detail.name || detail.task_id, detail.session_id)
    );
    actions.appendChild(openBtn);
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

    pane.appendChild(renderWorkingDir(detail));
    // Live Activity feed sits below the working-directory tree; it survives same-task re-renders
    // (ensureActivity keeps the existing node) so an in-flight run's events are never wiped.
    pane.appendChild(ensureActivity(detail.task_id));
    syncRunCtl(); // reflect any run state on the freshly rendered Run / Delete controls
  }

  // The task folder as a standard VS Code / Explorer-style vertical tree: rows reuse the Cloud
  // Drive .cd-* classes, children nest in .rtv-kids containers whose dotted border-left is the
  // per-level indent guide. The task folder row opens into its subfolders (materials/, outputs/,
  // any extra) and the root mirrors (task_spec.json / session_history.json). Folders start
  // collapsed so the main pane stays converged; ▸/▾ toggles expand in place and each file row
  // opens a preview. Folder rows stay on one line (name + (count) + "(empty)").
  function renderWorkingDir(detail) {
    const box = section("Working directory");
    const wrap = el("div", "rtv-tree-box");
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
    // Converged by default: the task folder row is expanded one level (so the work folders and
    // root mirrors are visible) but every subfolder starts collapsed; ▸/▾ expands in place.
    const open = new Set([""]);

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
      row.appendChild(el("span", "cd-tw"));
      row.appendChild(el("span", "cd-icon", fileTypeIcon(f)));
      const name = el("span", "cd-name", f.name);
      row.appendChild(name);
      row.appendChild(el("span", "cd-meta", formatBytes(f.size)));
      if (f.rag_status === "INDEXED") row.appendChild(el("span", "rtv-rag-tag", "KB"));
      else if (f.rag_status === "PENDING") row.appendChild(el("span", "rtv-rag-tag pending", "…"));
      row.title = `${f.folder_path ? f.folder_path + "/" : ""}${f.name}`;
      row.addEventListener("click", () => viewCloudFile(f));
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
    box.appendChild(wrap);
    return box;
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

  async function viewCloudFile(f) {
    const pane = document.getElementById("research-task-view");
    if (!pane) return;
    const old = pane.querySelector(".rtv-file-detail");
    if (old) old.remove();
    const det = el("div", "rtv-file-detail");
    const head = el("div", "rtv-file-detail-head");
    const back = el("button", "research-mini-btn", "✕");
    back.title = "Close";
    back.addEventListener("click", () => det.remove());
    head.appendChild(back);
    head.appendChild(el("div", "rtv-file-detail-title", `${f.folder_path ? f.folder_path + "/" : ""}${f.name}`));
    det.appendChild(head);
    det.appendChild(el("div", "research-status-empty", "Loading…"));
    pane.appendChild(det);
    let res;
    try {
      res = await apiFetch(`/files/${encodeURIComponent(f.id)}/content`);
    } catch (e) {
      det.lastChild.textContent = `Content unavailable: ${e.message}`;
      return;
    }
    const body = el("div", "rtv-file-detail-body");
    body.innerHTML = renderMarkdown(res.content);
    det.replaceChild(body, det.lastChild);
    det.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
