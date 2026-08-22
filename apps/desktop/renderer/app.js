// App wiring: folder picker + file tree, viewer dispatch, and the chat pane.
(() => {
  const treeEl = document.getElementById("tree");
  const chatLog = document.getElementById("chat-log");
  const chatForm = document.getElementById("chat-form");
  const chatInput = document.getElementById("chat-input");
  const chatSend = document.getElementById("chat-send");
  const chatTitle = document.getElementById("chat-title");
  const newChatBtn = document.getElementById("new-chat");

  const state = {
    sessionId: null,
    token: null,
    username: null,
    displayName: null,
    roleId: null,
    roleName: null,
    quota: null,
    email: null,
    phone: null,
    avatar: null,
    guestId: null,
    degradedNoticeShown: false,
    workspaceDir: null,
    treeData: null,
    sessions: null,
    sessionQuery: "",
  };
  try { state.token = localStorage.getItem("deepdive_token"); } catch { /* ignore */ }
  try { state.guestId = localStorage.getItem("deepdive_guest_id"); } catch { /* ignore */ }
  // Restore cached identity so the bottom bar shows the username immediately,
  // even before the backend revalidates the token.
  try {
    const cachedUser = JSON.parse(localStorage.getItem("deepdive_user") || "null");
    if (cachedUser) {
      state.username = cachedUser.username ?? null;
      state.displayName = cachedUser.displayName ?? null;
      state.roleId = cachedUser.roleId ?? null;
      state.roleName = cachedUser.roleName ?? null;
      state.email = cachedUser.email ?? null;
      state.phone = cachedUser.phone ?? null;
      state.avatar = cachedUser.avatar ?? null;
    }
  } catch { /* ignore */ }

  // ── File tree ──
  function buildNode(node, open = false) {
    if (node.type === "file") {
      const row = document.createElement("div");
      row.className = "tree-node file";
      row.textContent = node.name;
      row.title = node.path;
      const del = document.createElement("span");
      del.className = "tree-del";
      del.textContent = "🗑";
      del.title = "Delete from workspace";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteFileFromWorkspace(node.path);
      });
      row.appendChild(del);
      row.addEventListener("click", () => Viewer.render(node.path, node.name));
      return row;
    }
    const wrapper = document.createElement("div");
    const row = document.createElement("div");
    row.className = "tree-node dir";
    const toggle = document.createElement("span");
    toggle.className = "tree-toggle";
    toggle.textContent = open ? "▾" : "▸";
    row.appendChild(toggle);
    row.appendChild(document.createTextNode(node.name));
    wrapper.appendChild(row);

    const children = document.createElement("div");
    children.className = "tree-children";
    children.style.display = open ? "block" : "none";
    if (open) {
      for (const child of node.children || []) children.appendChild(buildNode(child, true));
    }
    row.addEventListener("click", () => {
      if (children.style.display === "none") {
        if (children.childElementCount === 0) {
          for (const child of node.children || []) children.appendChild(buildNode(child));
        }
        children.style.display = "block";
        toggle.textContent = "▾";
      } else {
        children.style.display = "none";
        toggle.textContent = "▸";
      }
    });
    wrapper.appendChild(children);
    return wrapper;
  }

  function renderTree(nodes, open = false) {
    treeEl.innerHTML = "";
    for (const node of nodes) treeEl.appendChild(buildNode(node, open));
  }

  // Fuzzy filter: keep files/dirs whose name contains the query; a dir is kept when
  // it matches or holds a matching descendant. An empty query restores the full tree.
  function filterNodes(nodes, q) {
    const out = [];
    for (const n of nodes) {
      if (n.type === "file") {
        if (n.name.toLowerCase().includes(q)) out.push(n);
      } else {
        const kids = filterNodes(n.children || [], q);
        if (n.name.toLowerCase().includes(q) || kids.length) {
          out.push({ ...n, children: kids });
        }
      }
    }
    return out;
  }

  function applyFileSearch(q) {
    q = (q || "").trim().toLowerCase();
    if (!state.treeData) return;
    if (!q) { renderTree(state.treeData); return; }
    renderTree(filterNodes(state.treeData, q), true);
  }

  async function loadTree(dir) {
    const tree = await window.desktopAPI.readTree(dir);
    state.treeData = tree;
    renderTree(tree);
  }

  async function pickFolder() {
    const dir = await window.desktopAPI.pickFolder();
    if (!dir) return;
    state.workspaceDir = dir;
    try { localStorage.setItem("deepdive_workspace_dir", dir); } catch { /* ignore */ }
    await loadTree(dir);
    reflectWorkspaceName();
  }

  // Show the open workspace's folder name in the sidebar button + the File menu label.
  function workspaceDisplayName(dir) {
    if (!dir) return "";
    const parts = dir.replace(/\\/g, "/").split("/").filter(Boolean);
    return parts[parts.length - 1] || dir;
  }

  function reflectWorkspaceName() {
    const name = workspaceDisplayName(state.workspaceDir);
    const btn = document.getElementById("pick-folder");
    if (btn) {
      btn.textContent = name ? `📁 ${name}` : "📁 Open Workspace";
      btn.title = state.workspaceDir || "";
    }
    if (window.desktopAPI.setWorkspaceLabel) window.desktopAPI.setWorkspaceLabel(name);
  }

  // File menu → "Add File to Workspace": pick a file, copy it into the open workspace,
  // then refresh the tree so it shows up immediately.
  async function addFileToWorkspace() {
    if (!state.workspaceDir) {
      Viewer.toast("Open a workspace folder first (📁 Open Workspace).");
      return;
    }
    const src = await window.desktopAPI.pickFile();
    if (!src) return;
    const res = await window.desktopAPI.copyIntoWorkspace(src, state.workspaceDir);
    if (!res.ok) { Viewer.toast(`Add failed: ${res.error}`); return; }
    await loadTree(state.workspaceDir);
    Viewer.toast(`Added ${res.name} to workspace.`);
  }

  // Delete a file from the open workspace (confirm first, then refresh the tree).
  async function deleteFileFromWorkspace(filePath) {
    if (!state.workspaceDir) return;
    const name = filePath.replace(/\\/g, "/").split("/").filter(Boolean).pop() || filePath;
    if (!confirm(`Delete "${name}" from the workspace?\nThis permanently removes the file.`)) return;
    const res = await window.desktopAPI.deleteFile(filePath, state.workspaceDir);
    if (!res.ok) { Viewer.toast(`Delete failed: ${res.error}`); return; }
    await loadTree(state.workspaceDir);
    Viewer.toast(`Deleted ${res.name}.`);
  }

  // ── Chat ──
  // Plain message for error/notice rows (no per-message actions).
  function appendMsg(role, text) {
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    div.textContent = text;
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
    return div;
  }

  // User/assistant message with a per-message action row: copy / read aloud / delete.
  function appendMessage(id, role, text) {
    if (role !== "user" && role !== "assistant") return appendMsg(role, text);
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    if (id) div.dataset.id = id;
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.textContent = text;
    div.appendChild(bubble);
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    const buttons = [
      ["copy", "📋", "Copy", () => copyText(text)],
      ["speak", "🔊", "Read aloud", () => speakMessage(text)],
      ["delete", "🗑", "Delete", () => deleteMessage(div)],
    ];
    for (const [a, glyph, title, fn] of buttons) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.a = a;
      b.title = title;
      b.textContent = glyph;
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        if (a === "speak") speakMessage(text, b);
        else fn();
      });
      actions.appendChild(b);
    }
    div.appendChild(actions);
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
    return div;
  }

  // Streaming assistant message: same DOM as appendMessage, but the copy/speak buttons close
  // over a mutable text variable so they read the live (and final) answer during streaming.
  // `add` appends a delta; `reset` starts a fresh bubble when a new agent step begins.
  function appendStreamingAssistant() {
    const div = document.createElement("div");
    div.className = "msg assistant";
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    let text = "";
    const scroll = () => { chatLog.scrollTop = chatLog.scrollHeight; };
    const buttons = [
      ["copy", "📋", "Copy", () => copyText(text)],
      ["speak", "🔊", "Read aloud", () => speakMessage(text)],
      ["delete", "🗑", "Delete", () => deleteMessage(div)],
    ];
    for (const [a, glyph, title, fn] of buttons) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.a = a;
      b.title = title;
      b.textContent = glyph;
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        if (a === "speak") speakMessage(text, b);
        else fn();
      });
      actions.appendChild(b);
    }
    div.appendChild(bubble);
    div.appendChild(actions);
    chatLog.appendChild(div);
    return {
      el: div,
      add: (t) => { text += t; bubble.textContent = text; scroll(); },
      reset: () => { text = ""; bubble.textContent = ""; },
    };
  }

  // Collapsible status bar shown while the model reasons / uses tools. Collapsed by default,
  // so only a single dynamic line is visible ("💭 思考中…", "🔍 正在搜索…", …) — the full
  // streamed reasoning + tool activity is hidden until the line is clicked. Once the answer
  // starts, the summary settles on a static "思考过程" label, still collapsed, so the answer
  // is the main content below.
  const TOOL_LABELS = {
    rag_search: "📚 Querying rag…",
    web_search: "🔍 Searching web…",
    translate: "🌐 Translating…",
  };
  function makeStatusBar() {
    const details = document.createElement("details");
    details.className = "msg-thinking";
    const sum = document.createElement("summary");
    const box = document.createElement("div");
    box.className = "thinking-text";
    details.appendChild(sum);
    details.appendChild(box);
    chatLog.appendChild(details);
    const scroll = () => { chatLog.scrollTop = chatLog.scrollHeight; };
    return {
      el: details,
      setPhase: (txt) => { sum.textContent = txt; },
      addThinking: (t) => { box.textContent += t; scroll(); },
      addTool: (label) => { box.textContent += (box.textContent ? "\n" : "") + label; },
      done: () => { sum.textContent = "💭 Thoughts"; scroll(); },
    };
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      Viewer.toast("Copied to clipboard.");
    } catch {
      Viewer.toast("Copy failed.");
    }
  }

  // Sequential voice playback queue: segments are appended as the /tts/stream SSE feeds them,
  // and each segment plays only after the previous one finished. `voiceStop` cancels any
  // pending/current playback and aborts the in-flight TTS fetch. `voiceGen` tags each read so
  // segments from a superseded stream are ignored instead of re-queued (which previously caused
  // the same text to be read twice when the button was re-clicked).
  const voiceQueue = [];
  let voiceCurrent = null;
  let voiceGen = 0;
  let voiceAbort = null;

  // Highlight the 🔊 button that is currently reading (only one read runs at a time).
  function setVoiceBtn(btn) {
    document.querySelectorAll("#chat-log .msg-actions button[data-a='speak']").forEach((b) => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
  }
  function voiceStop() {
    voiceGen++;
    voiceQueue.length = 0;
    if (voiceCurrent) { voiceCurrent.pause(); voiceCurrent = null; }
    if (voiceAbort) { voiceAbort.abort(); voiceAbort = null; }
    setVoiceBtn(null);
  }
  function voicePlay() {
    if (voiceCurrent || !voiceQueue.length) return;
    const url = voiceQueue.shift();
    const a = new Audio(url);
    voiceCurrent = a;
    const next = () => {
      if (voiceCurrent !== a) return;
      voiceCurrent = null;
      if (!voiceQueue.length) setVoiceBtn(null);
      voicePlay();
    };
    a.onended = next;
    a.onerror = next;
    a.play().catch(next);
  }

  // Read a message aloud via the streaming server-side Kokoro TTS (POST /tts/stream → SSE
  // segment URLs played in sequence, so audio starts as soon as the first sentence is ready).
  // Clicking the 🔊 of the message that is already reading stops it instead of re-reading.
  async function speakMessage(text, btn) {
    if (btn && btn.classList.contains("active")) { voiceStop(); return; }
    voiceStop();
    const gen = voiceGen;
    setVoiceBtn(btn);
    voiceAbort = new AbortController();
    try {
      const res = await fetch("/api/tts/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
        signal: voiceAbort.signal,
      });
      if (!res.ok) throw new Error(`${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let segments = 0;
      let gotDone = false;
      let fatal = null;
      const processBlock = (block) => {
        for (const line of block.split("\n")) {
          const l = line.trim();
          if (!l.startsWith("data:")) continue;
          const raw = l.slice(5).trim();
          if (raw === "[DONE]") continue;
          let evt;
          try { evt = JSON.parse(raw); } catch { continue; }
          if (evt.type === "segment") { segments++; voiceQueue.push(evt.url); voicePlay(); }
          else if (evt.type === "done") gotDone = true;
          else if (evt.type === "error") fatal = evt.detail || "TTS error";
        }
      };
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (voiceGen !== gen) return; // superseded by a newer read/stop → ignore this stream
        buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) { processBlock(buf.slice(0, idx)); buf = buf.slice(idx + 2); }
      }
      if (buf.trim()) processBlock(buf);
      if (voiceGen !== gen) return;
      if (fatal) throw new Error(fatal);
      if (segments === 0) throw new Error("no audio returned");
      if (!gotDone) Viewer.toast("TTS stream ended early.");
    } catch (err) {
      if (err.name === "AbortError") return; // intentional stop, not a failure
      if (voiceGen === gen) { voiceStop(); Viewer.toast(`TTS failed: ${err.message}`); }
    }
  }

  // Delete a single message (server-side). With no id (e.g. old client state) it only
  // removes the bubble from the view.
  async function deleteMessage(msgEl) {
    const id = msgEl.dataset.id;
    if (!state.sessionId || !id) {
      msgEl.remove();
      return;
    }
    try {
      const res = await fetch(`/api/sessions/${state.sessionId}/messages/${id}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (res.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
      if (!res.ok) throw new Error(`${res.status}`);
      msgEl.remove();
      loadSessions();
    } catch (err) {
      appendMsg("error", `Delete failed: ${err.message}`);
    }
  }

  function authHeaders() {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    return headers;
  }

  async function sendChat(message) {
    const userMsgEl = appendMessage(null, "user", message);
    const hadSession = !!state.sessionId;
    chatSend.disabled = true;
    const payload = { message, session_id: state.sessionId ?? undefined };
    if (!state.token) payload.user_id = state.guestId ?? undefined;

    // Streaming state: status bar + assistant bubble are created lazily on first event.
    let statusBar = null;      // collapsible reasoning/tool status line
    let streamMsg = null;      // { el, add, reset }
    let stepChanged = false;   // a new agent step began → restart the bubble on next content
    let gotDone = false;

    const handleEvent = (evt) => {
      switch (evt.type) {
        case "notice":
          appendMsg("notice", evt.data);
          break;
        case "thinking":
          if (!statusBar) statusBar = makeStatusBar();
          statusBar.setPhase("💭 Thinking…");
          statusBar.addThinking(evt.data);
          break;
        case "tool": {
          if (!statusBar) statusBar = makeStatusBar();
          const label = TOOL_LABELS[evt.data?.name] || `⚙️ ${evt.data?.name || "tool"}…`;
          statusBar.setPhase(label);
          statusBar.addTool(label);
          break;
        }
        case "step-answer":
          stepChanged = true; // a tool round's answer boundary; next content starts fresh
          break;
        case "content":
          if (statusBar) statusBar.done();
          if (!streamMsg) streamMsg = appendStreamingAssistant();
          if (stepChanged) { streamMsg.reset(); stepChanged = false; }
          streamMsg.add(evt.data);
          break;
        case "done":
          gotDone = true;
          if (statusBar) statusBar.done(); // settle the label even on tool-only rounds
          state.sessionId = evt.data.session_id ?? state.sessionId;
          if (!state.token && evt.data.user_id) {
            state.guestId = evt.data.user_id;
            try { localStorage.setItem("deepdive_guest_id", evt.data.user_id); } catch { /* ignore */ }
          }
          if (evt.data.notice && !state.degradedNoticeShown) {
            appendMsg("notice", evt.data.notice);
            state.degradedNoticeShown = true;
          }
          if (evt.data.user_message_id) userMsgEl.dataset.id = evt.data.user_message_id;
          if (evt.data.assistant_message_id && streamMsg) streamMsg.el.dataset.id = evt.data.assistant_message_id;
          // Fallback when no content streamed (e.g. tool round produced only reasoning).
          if (!streamMsg && evt.data.answer) {
            streamMsg = appendStreamingAssistant();
            streamMsg.add(evt.data.answer);
          }
          if (evt.data.answer && speakEnabled) speak(evt.data.answer);
          // Fresh session: show a provisional title (first words) until the worker's async
          // LLM title lands; then refresh the list so the auto-named session shows up.
          if (!hadSession) {
            const cut = message.slice(0, 30);
            chatTitle.textContent = cut + (message.length > 30 ? "…" : "");
          }
          if (state.token) { loadSessions(); setTimeout(loadSessions, 3000); }
          break;
      }
    };

    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(payload),
      });
      if (res.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
      if (res.status === 429) {
        const body = await res.json().catch(() => ({}));
        if (!state.token) {
          appendMsg("error", body.detail || "Guest limit reached — sign in to keep chatting.");
          openAccount();
          return;
        }
        throw new Error(body.detail || "Daily chat limit reached.");
      }
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);

      // Parse the SSE stream: each event is a JSON "data: {...}" block separated by blank lines.
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      const processBlock = (block) => {
        for (const line of block.split("\n")) {
          const l = line.trim();
          if (!l.startsWith("data:")) continue;
          const raw = l.slice(5).trim();
          if (raw === "[DONE]") continue;
          try { handleEvent(JSON.parse(raw)); } catch { /* ignore malformed frames */ }
        }
      };
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          processBlock(buf.slice(0, idx));
          buf = buf.slice(idx + 2);
        }
      }
      if (buf.trim()) processBlock(buf);
      if (!gotDone) {
        if (streamMsg) appendMsg("notice", "连接中断,已显示部分回答。");
        else appendMsg("error", "连接中断,未收到完整回答。");
      }
    } catch (err) {
      appendMsg("error", `Request failed: ${err.message}`);
    } finally {
      chatSend.disabled = false;
    }
  }

  // New chat: drop the active session reference and clear the pane.
  function newChat() {
    state.sessionId = null;
    chatLog.innerHTML = "";
    chatTitle.textContent = "New chat";
    if (state.token) loadSessions();
  }

  // Delete a whole session from the sidebar list.
  async function deleteSession(id) {
    try {
      const res = await fetch(`/api/sessions/${id}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (res.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
      if (!res.ok) throw new Error(`${res.status}`);
      if (state.sessionId === id) newChat();
      loadSessions();
    } catch (err) {
      Viewer.toast(`Delete session failed: ${err.message}`);
    }
  }

  // Persist a renamed session title; an empty name resets it so auto-naming reapplies.
  async function renameSession(id, title) {
    const res = await fetch(`/api/sessions/${id}`, {
      method: "PATCH",
      headers: authHeaders(),
      body: JSON.stringify({ title }),
    });
    if (res.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
    if (!res.ok) throw new Error(`${res.status}`);
    return (await res.json()).title;
  }

  // Click the chat-header title to rename the active session inline.
  function startTitleEdit() {
    if (!state.sessionId || !state.token) return;
    const current = chatTitle.textContent;
    const input = document.createElement("input");
    input.type = "text";
    input.value = current;
    input.className = "chat-title-input";
    input.maxLength = 80;
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      input.remove();
      chatTitle.style.display = "";
      if (commit) {
        const next = input.value.trim();
        if (next !== current) {
          try {
            const saved = await renameSession(state.sessionId, next);
            chatTitle.textContent = saved || next || "New chat";
            loadSessions();
          } catch (err) {
            chatTitle.textContent = current;
            appendMsg("error", `Rename failed: ${err.message}`);
          }
          return;
        }
      }
      chatTitle.textContent = current;
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(true);
      else if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
    chatTitle.style.display = "none";
    chatTitle.parentNode.insertBefore(input, chatTitle.nextSibling);
    input.focus();
    input.select();
  }
  chatTitle.addEventListener("click", startTitleEdit);

  // Rename a session from its row in the sidebar list.
  function startSessionRename(s, summaryEl) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = s.title || s.summary || s.id.slice(0, 8);
    input.className = "session-rename-input";
    input.maxLength = 80;
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      if (commit) {
        const next = input.value.trim();
        if (next !== (s.title || "")) {
          try {
            const saved = await renameSession(s.id, next);
            if (state.sessionId === s.id) chatTitle.textContent = saved || next || "New chat";
          } catch (err) {
            Viewer.toast(`Rename failed: ${err.message}`);
          }
        }
      }
      loadSessions();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(true);
      else if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
    summaryEl.replaceWith(input);
    input.focus();
    input.select();
  }

  // ── Media generation (video → PPT / PDF book) ──
  async function pollJob(jobId) {
    for (let i = 0; i < 900; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const res = await fetch(`/api/jobs/${jobId}`);
      const job = await res.json();
      if (job.status === "succeeded") {
        Viewer.toast(`Generated: ${job.result.path}`);
        window.desktopAPI.openExternal(job.result.path);
        return;
      }
      if (job.status === "failed") {
        Viewer.toast(`Generation failed: ${job.error}`);
        return;
      }
    }
    Viewer.toast("Generation timed out; check the output folder later.");
  }

  window.generateMedia = async (filePath, name, format) => {
    const subtitle = await window.desktopAPI.findSubtitle(filePath);
    Viewer.toast("Generation task submitted, processing…");
    try {
      const res = await fetch("/api/media/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          video_path: filePath,
          subtitle_path: subtitle,
          format,
        }),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const { job_id } = await res.json();
      pollJob(job_id);
    } catch (err) {
      Viewer.toast(`Generation failed: ${err.message}`);
    }
  };

  // ── User menu + profile modal + preferences + login modal ──
  const userMenu = document.getElementById("user-menu");
  const userMenuTrigger = document.getElementById("user-menu-trigger");
  const userAvatar = document.getElementById("user-avatar");
  const userName = document.getElementById("user-name");
  const profileMenuValue = document.getElementById("profile-menu-value");
  const userMenuAdminGroup = document.getElementById("user-menu-admin-group");
  const userMenuSignoutGroup = document.getElementById("user-menu-signout-group");
  const accountOverlay = document.getElementById("account-overlay");
  const profileOverlay = document.getElementById("profile-overlay");
  const profileAvatar = document.getElementById("profile-avatar");
  const profileName = document.getElementById("profile-name");
  const profileUsername = document.getElementById("profile-username");
  const profileRole = document.getElementById("profile-role");
  const profilePlan = document.getElementById("profile-plan");
  const profileTokenLimit = document.getElementById("profile-token-limit");
  const profileRequestLimit = document.getElementById("profile-request-limit");
  const profileRpm = document.getElementById("profile-rpm");
  const profileModel = document.getElementById("profile-model");
  const accountUser = document.getElementById("account-user");
  const accountPass = document.getElementById("account-pass");
  const accountStatus = document.getElementById("account-status");
  const accountRemember = document.getElementById("account-remember");
  const themeOptions = document.querySelectorAll("[data-theme]");
  const userMenuItems = document.querySelectorAll("#user-menu [data-action]");
  const settingsOverlay = document.getElementById("settings-overlay");
  const settingsTabs = document.querySelectorAll("#settings-tabs .settings-tab");
  const settingsTabBodies = document.querySelectorAll(".settings-tab-body");
  const updatesVersion = document.getElementById("updates-version");
  const aboutVersion = document.getElementById("about-version");
  const checkUpdatesBtn = document.getElementById("check-updates-btn");
  const updateStatus = document.getElementById("update-status");
  const updateOpenRelease = document.getElementById("update-open-release");
  const accentSwatches = document.getElementById("accent-swatches");
  const rememberBoundsRow = document.querySelector("[data-pref='rememberBounds']");
  const fontSizeSelect = document.getElementById("pref-font-size");
  const monoSelect = document.getElementById("pref-mono-font");
  const zoomSelect = document.getElementById("pref-zoom");
  const feedbackCategory = document.getElementById("feedback-category");
  const feedbackText = document.getElementById("feedback-text");
  const feedbackLogs = document.getElementById("feedback-logs");
  const registerOverlay = document.getElementById("register-overlay");
  const registerStatus = document.getElementById("register-status");
  const registerDebug = document.getElementById("register-debug");
  const registerDebugInput = document.getElementById("register-debug-input");
  const registerDebugCopy = document.getElementById("register-debug-copy");
  const forgotOverlay = document.getElementById("forgot-overlay");
  const forgotStatus = document.getElementById("forgot-status");
  const forgotDebug = document.getElementById("forgot-debug");
  const forgotDebugInput = document.getElementById("forgot-debug-input");
  const forgotDebugCopy = document.getElementById("forgot-debug-copy");
  const profileAvatarImg = document.getElementById("profile-avatar-img");
  const profileAvatarLetter = document.getElementById("profile-avatar-letter");
  const profileFieldDisplay = document.getElementById("profile-field-display");
  const profileFieldUsername = document.getElementById("profile-field-username");
  const profileFieldEmail = document.getElementById("profile-field-email");
  const profileFieldPhone = document.getElementById("profile-field-phone");
  const profileFieldCurpass = document.getElementById("profile-field-curpass");
  const profileFieldNewpass = document.getElementById("profile-field-newpass");
  const profileFieldNewpass2 = document.getElementById("profile-field-newpass2");
  const profileEditStatus = document.getElementById("profile-edit-status");
  const profileEditDebug = document.getElementById("profile-edit-debug");
  const profileEditDebugInput = document.getElementById("profile-edit-debug-input");
  const profileEditDebugCopy = document.getElementById("profile-edit-debug-copy");
  const profileAvatarStatus = document.getElementById("profile-avatar-status");

  const TOKEN_KEY = "deepdive_token";
  const USER_KEY = "deepdive_user";
  // Last signed-in username, kept across restarts *and* logouts so the login modal
  // can pre-fill it (the "remember username" half of "Keep me signed in").
  const REMEMBER_USER_KEY = "deepdive_remember_user";
  const THEME_KEY = "deepdive_theme";
  const ACCENT_KEY = "deepdive_accent";
  const FONT_SIZE_KEY = "deepdive_font_size";
  const MONO_KEY = "deepdive_mono_font";
  const ZOOM_KEY = "deepdive_zoom";
  const CHANNEL_KEY = "deepdive_channel";

  // ── Appearance prefs (theme / accent) ──
  const systemMedia = window.matchMedia("(prefers-color-scheme: dark)");
  function effectiveTheme(theme) {
    return theme === "system" ? (systemMedia.matches ? "dark" : "light") : theme;
  }
  function applyTheme(theme) {
    document.documentElement.dataset.theme = effectiveTheme(theme);
    themeOptions.forEach((row) => {
      row.classList.toggle("active", row.dataset.theme === theme);
    });
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* ignore */ }
  }
  try { applyTheme(localStorage.getItem(THEME_KEY) || "dark"); } catch { /* ignore */ }
  systemMedia.addEventListener("change", () => {
    try {
      if ((localStorage.getItem(THEME_KEY) || "dark") === "system") applyTheme("system");
    } catch { /* ignore */ }
  });

  const ACCENTS = ["#5b8cff", "#7c5cff", "#2fb675", "#e0a13c", "#e05b7c"];
  function applyAccent(color) {
    document.documentElement.style.setProperty("--accent", color);
    document.querySelectorAll(".accent-swatch").forEach((sw) => sw.classList.toggle("active", sw.dataset.color === color));
    try { localStorage.setItem(ACCENT_KEY, color); } catch { /* ignore */ }
  }
  ACCENTS.forEach((color) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "accent-swatch";
    b.dataset.color = color;
    b.style.background = color;
    b.title = color;
    accentSwatches.appendChild(b);
    b.addEventListener("click", () => applyAccent(color));
  });
  try { applyAccent(localStorage.getItem(ACCENT_KEY) || ACCENTS[0]); } catch { /* ignore */ }

  // ── Window & Display prefs (font size / mono font / zoom / bounds) ──
  // Compose "font size" and "UI scale" into one zoom factor so both settings
  // visibly scale the whole UI, including the chat pane and the viewer.
  function applyUiScale() {
    const fsPx = parseInt(localStorage.getItem(FONT_SIZE_KEY), 10) || 14;
    const uiZoom = parseFloat(localStorage.getItem(ZOOM_KEY)) || 1;
    document.body.style.zoom = String(uiZoom * (fsPx / 14));
  }
  function applyFontSize(px) {
    if (fontSizeSelect) fontSizeSelect.value = String(px);
    try { localStorage.setItem(FONT_SIZE_KEY, String(px)); } catch { /* ignore */ }
    applyUiScale();
  }
  try { applyFontSize(parseInt(localStorage.getItem(FONT_SIZE_KEY), 10) || 14); } catch { /* ignore */ }

  function applyMonoFont(f) {
    if (f === "default") document.documentElement.style.removeProperty("--mono");
    else document.documentElement.style.setProperty("--mono", `${f}, ui-monospace, "Cascadia Code", Consolas, monospace`);
    if (monoSelect) monoSelect.value = f;
    try { localStorage.setItem(MONO_KEY, f); } catch { /* ignore */ }
  }
  try { applyMonoFont(localStorage.getItem(MONO_KEY) || "default"); } catch { /* ignore */ }

  function applyZoom(z) {
    if (zoomSelect) zoomSelect.value = String(z);
    try { localStorage.setItem(ZOOM_KEY, String(z)); } catch { /* ignore */ }
    applyUiScale();
  }
  try { applyZoom(parseFloat(localStorage.getItem(ZOOM_KEY)) || 1); } catch { /* ignore */ }

  function applyRememberBounds(on) {
    rememberBoundsRow.classList.toggle("active", !!on);
    try { localStorage.setItem("deepdive_remember_bounds", on ? "1" : "0"); } catch { /* ignore */ }
    if (window.desktopAPI && window.desktopAPI.setPref) {
      window.desktopAPI.setPref("window.rememberBounds", !!on).catch(() => {});
    }
  }
  try { applyRememberBounds(localStorage.getItem("deepdive_remember_bounds") !== "0"); } catch { /* ignore */ }

  // ── Updates pref (distribution channel) ──
  function applyChannel(ch) {
    document.querySelectorAll("[data-channel]").forEach((row) => {
      row.classList.toggle("active", row.dataset.channel === ch);
    });
    try { localStorage.setItem(CHANNEL_KEY, ch); } catch { /* ignore */ }
  }
  try { applyChannel(localStorage.getItem(CHANNEL_KEY) || "stable"); } catch { /* ignore */ }

  function setAccountStatus(text, cls) {
    accountStatus.textContent = text;
    accountStatus.className = "cfg-status" + (cls ? " " + cls : "");
  }

  // No avatar field exists on the backend; use an initial-letter circle.
  function avatarLetter() {
    const name = state.displayName || state.username || "";
    return name ? name.trim().charAt(0).toUpperCase() : "👤";
  }

  function closeUserMenu() {
    userMenu.classList.add("hidden");
  }
  function closeSettingsModal() {
    settingsOverlay.classList.add("hidden");
  }
  function closeAccount() {
    accountOverlay.classList.add("hidden");
  }
  function closeProfile() {
    profileOverlay.classList.add("hidden");
  }
  function closeRegister() {
    registerOverlay.classList.add("hidden");
  }
  function closeForgot() {
    forgotOverlay.classList.add("hidden");
  }
  function showDebugLink(box, input, url) {
    if (!url) { box.classList.add("hidden"); return; }
    input.value = url;
    box.classList.remove("hidden");
  }
  async function copyToClipboard(text, statusEl) {
    try {
      await navigator.clipboard.writeText(text);
      statusEl.textContent = "链接已复制";
      statusEl.className = "cfg-status ok";
    } catch {
      statusEl.textContent = "复制失败,请手动选择链接复制";
      statusEl.className = "cfg-status err";
    }
  }

  function toggleUserMenu() {
    userMenu.classList.toggle("hidden");
  }

  // Refresh the bottom-bar trigger, menu Profile value, and per-role group visibility.
  function renderUserMenu() {
    const loggedIn = !!state.token;
    userAvatar.textContent = loggedIn ? avatarLetter() : "👤";
    userName.textContent = loggedIn ? (state.displayName || state.username || "Signed in") : "Sign in";
    profileMenuValue.textContent = loggedIn ? (state.displayName || state.username || "") : "Sign in";
    userMenuAdminGroup.classList.toggle("hidden", !(loggedIn && state.roleId === "admin"));
    userMenuSignoutGroup.classList.toggle("hidden", !loggedIn);
  }

  async function refreshProfile() {
    if (!state.token) return;
    try {
      const res = await fetch("/api/auth/me", { headers: authHeaders() });
      if (res.ok) {
        const me = await res.json();
        state.username = me.username;
        state.displayName = me.display_name;
        state.roleId = me.role_id;
        state.roleName = me.role_name;
        state.quota = me.quota || null;
        state.email = me.email || null;
        state.phone = me.phone || null;
        state.avatar = me.avatar || null;
        try {
          localStorage.setItem(USER_KEY, JSON.stringify({
            username: me.username,
            displayName: me.display_name,
            roleId: me.role_id,
            roleName: me.role_name,
            email: me.email,
            phone: me.phone,
            avatar: me.avatar,
          }));
        } catch { /* ignore */ }
        renderUserMenu();
      } else if (res.status === 401) {
        logout();
      }
    } catch { /* backend unreachable; keep cached info */ }
  }

  function renderHeadAvatar() {
    if (state.avatar) {
      fetch("/api" + state.avatar, { headers: authHeaders() })
        .then((r) => (r.ok ? r.blob() : Promise.reject(new Error("load failed"))))
        .then((blob) => {
          const url = URL.createObjectURL(blob);
          profileAvatar.innerHTML = `<img src="${url}" class="avatar-img" alt="avatar" />`;
        })
        .catch(() => { profileAvatar.textContent = avatarLetter(); });
    } else {
      profileAvatar.innerHTML = "";
      profileAvatar.textContent = avatarLetter();
    }
  }

  let avatarObjectUrl = null;
  function revokeAvatar() {
    if (avatarObjectUrl) { URL.revokeObjectURL(avatarObjectUrl); avatarObjectUrl = null; }
  }
  // The edit-form avatar: fetch via the /api proxy → blob → object URL, so the
  // CSP img-src restriction never applies and any image size works.
  function renderAvatar() {
    revokeAvatar();
    profileAvatarStatus.textContent = "";
    profileAvatarStatus.className = "cfg-status";
    if (state.avatar) {
      fetch("/api" + state.avatar, { headers: authHeaders() })
        .then((r) => (r.ok ? r.blob() : Promise.reject(new Error("load failed"))))
        .then((blob) => {
          avatarObjectUrl = URL.createObjectURL(blob);
          profileAvatarImg.src = avatarObjectUrl;
          profileAvatarImg.classList.remove("hidden");
          profileAvatarLetter.classList.add("hidden");
        })
        .catch(() => {
          profileAvatarImg.classList.add("hidden");
          profileAvatarLetter.classList.remove("hidden");
          profileAvatarLetter.textContent = avatarLetter();
        });
    } else {
      profileAvatarImg.classList.add("hidden");
      profileAvatarLetter.classList.remove("hidden");
      profileAvatarLetter.textContent = avatarLetter();
    }
  }

  function renderProfile() {
    renderHeadAvatar();
    profileName.textContent = state.displayName || state.username || "—";
    profileUsername.textContent = state.username ? "@" + state.username : "";
    profileRole.textContent = state.roleName || "";
    profileRole.className = "tier" + (state.roleName === "vip" || state.roleName === "admin" ? " vip" : "");
    const q = state.quota || {};
    const fmt = (v) => (v === undefined ? "—" : v < 0 ? "Unlimited" : v.toLocaleString());
    profilePlan.textContent = q.role_name || state.roleName || "—";
    profileTokenLimit.textContent = fmt(q.daily_token_limit);
    profileRequestLimit.textContent = fmt(q.daily_request_limit);
    profileRpm.textContent = fmt(q.rpm_limit);
    profileModel.textContent = q.default_model || "—";
  }

  function openProfile() {
    renderProfile();
    profileFieldDisplay.value = state.displayName || "";
    profileFieldUsername.value = state.username || "";
    profileFieldEmail.value = state.email || "";
    profileFieldPhone.value = state.phone || "";
    profileFieldCurpass.value = "";
    profileFieldNewpass.value = "";
    profileFieldNewpass2.value = "";
    profileEditStatus.textContent = "";
    profileEditStatus.className = "cfg-status";
    profileEditDebug.classList.add("hidden");
    renderAvatar();
    closeUserMenu();
    profileOverlay.classList.remove("hidden");
  }

  function openUrl(url) {
    if (window.desktopAPI && window.desktopAPI.openUrl) {
      return window.desktopAPI.openUrl(url).catch(() => {});
    }
    window.open(url, "_blank");
    return Promise.resolve();
  }

  function openSettingsModal() {
    closeUserMenu();
    settingsOverlay.classList.remove("hidden");
    if (window.desktopAPI && window.desktopAPI.getAppVersion) {
      window.desktopAPI
        .getAppVersion()
        .then((v) => {
          if (v) {
            if (updatesVersion) updatesVersion.textContent = v;
            if (aboutVersion) aboutVersion.textContent = v;
          }
        })
        .catch(() => {});
    }
  }

  function showSettingsTab(name) {
    settingsTabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.settingsTab === name));
    settingsTabBodies.forEach((body) => body.classList.toggle("hidden", body.id !== "settings-tab-" + name));
  }
  showSettingsTab("appearance");

  let latestReleaseUrl = null;
  async function checkForUpdates() {
    if (!window.desktopAPI || !window.desktopAPI.checkUpdate) {
      updateStatus.textContent = "Update check is not available in this build.";
      return;
    }
    checkUpdatesBtn.disabled = true;
    updateOpenRelease.classList.add("hidden");
    updateStatus.textContent = "Checking for updates…";
    try {
      const r = await window.desktopAPI.checkUpdate();
      if (!r.ok) {
        updateStatus.textContent = `Check failed: ${r.error}`;
        return;
      }
      if (r.status === "latest") {
        updateStatus.textContent = `已是最新版本 (v${r.current})`;
      } else {
        latestReleaseUrl = r.url;
        updateStatus.textContent = `发现新版本 v${r.latest}(当前 v${r.current})`;
        updateOpenRelease.classList.remove("hidden");
      }
    } finally {
      checkUpdatesBtn.disabled = false;
    }
  }

  function submitFeedback() {
    const text = feedbackText.value.trim();
    if (!text) { feedbackText.focus(); return; }
    const catLabel = { feature: "功能建议", bug: "缺陷报告", ai: "AI 生成异常" }[feedbackCategory.value] || feedbackCategory.value;
    const diag = feedbackLogs.checked
      ? `\n\nOS: ${navigator.platform} · App: ${aboutVersion.textContent || "?"}`
      : "";
    const url =
      "https://github.com/Eric-LLMs/DeepDive/issues/new?" +
      `title=${encodeURIComponent(`[Feedback] ${catLabel}`)}&` +
      `body=${encodeURIComponent(`**类别:** ${catLabel}\n\n${text}${diag}`)}`;
    openUrl(url);
    feedbackText.value = "";
  }

  // Open the web console with a fresh stateless session token. SSO hands the desktop's
  // API token to the browser, but that token is rotated out by the next login — exchange
  // it for a signed cc_ console session first so the browser tab stays signed in.
  async function openWebConsole(hash) {
    closeUserMenu();
    let session = state.token;
    if (state.token) {
      try {
        const res = await fetch("/api/auth/session", {
          method: "POST",
          headers: authHeaders(),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.access_token) session = data.access_token;
        }
      } catch { /* keep the raw API token as a fallback */ }
    }
    openUrl(
      session
        ? `http://localhost:5173/?sso=${encodeURIComponent(session)}${hash}`
        : `http://localhost:5173/${hash}`
    );
  }

  function handleUserAction(action) {
    switch (action) {
      case "profile":
        closeUserMenu();
        if (state.token) openProfile();
        else openAccount();
        break;
      case "web-console":
        openWebConsole("");
        break;
      case "cloud-drive":
        // #drive selects the Cloud Drive tab once the console loads.
        openWebConsole("#drive");
        break;
      case "admin-console":
        closeUserMenu();
        if (state.roleId === "admin") openUrl("http://localhost:8300/admin");
        break;
      case "settings":
        openSettingsModal();
        break;
      case "signout":
        closeUserMenu();
        logout();
        break;
      // check-updates is handled elsewhere; unknown actions are no-ops.
    }
  }

  function openAccount() {
    setAccountStatus("");
    // Re-fill the last signed-in username (remember-username) so a returning user only
    // types the password. Survives logouts, unlike the session token/USER_KEY.
    if (!accountUser.value) {
      try { accountUser.value = localStorage.getItem(REMEMBER_USER_KEY) || ""; } catch { /* ignore */ }
    }
    accountPass.value = "";
    closeUserMenu();
    closeRegister();
    closeForgot();
    accountOverlay.classList.remove("hidden");
  }

  function openRegister() {
    setAccountStatus("");
    closeAccount();
    closeForgot();
    registerStatus.textContent = "";
    registerStatus.className = "cfg-status";
    registerDebug.classList.add("hidden");
    registerOverlay.classList.remove("hidden");
    document.getElementById("reg-username").focus();
  }

  function openForgot() {
    setAccountStatus("");
    closeAccount();
    closeRegister();
    forgotStatus.textContent = "";
    forgotStatus.className = "cfg-status";
    forgotDebug.classList.add("hidden");
    forgotOverlay.classList.remove("hidden");
    document.getElementById("forgot-email").focus();
  }

  async function register() {
    const username = document.getElementById("reg-username").value.trim();
    const email = document.getElementById("reg-email").value.trim();
    const display = document.getElementById("reg-display").value.trim();
    const password = document.getElementById("reg-password").value;
    const password2 = document.getElementById("reg-password2").value;
    registerStatus.textContent = "";
    registerStatus.className = "cfg-status";
    registerDebug.classList.add("hidden");
    if (!username || !email || !password) {
      registerStatus.textContent = "请填写用户名、邮箱和密码";
      registerStatus.className = "cfg-status err";
      return;
    }
    if (password !== password2) {
      registerStatus.textContent = "两次输入的密码不一致";
      registerStatus.className = "cfg-status err";
      return;
    }
    const btn = document.getElementById("register-submit");
    btn.disabled = true;
    try {
      const res = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, email, password, display_name: display || undefined }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `注册失败 (${res.status})`);
      showDebugLink(registerDebug, registerDebugInput, data.debug_verify_url);
      registerStatus.textContent = data.message || "注册成功,请查收邮件完成邮箱验证。";
      registerStatus.className = "cfg-status ok";
      document.getElementById("reg-password").value = "";
      document.getElementById("reg-password2").value = "";
      accountUser.value = username;
    } catch (err) {
      registerStatus.textContent = err.message;
      registerStatus.className = "cfg-status err";
    } finally {
      btn.disabled = false;
    }
  }

  async function forgotPassword() {
    const email = document.getElementById("forgot-email").value.trim();
    forgotStatus.textContent = "";
    forgotStatus.className = "cfg-status";
    forgotDebug.classList.add("hidden");
    if (!email) {
      forgotStatus.textContent = "请输入邮箱";
      forgotStatus.className = "cfg-status err";
      return;
    }
    const btn = document.getElementById("forgot-submit");
    btn.disabled = true;
    try {
      const res = await fetch("/api/auth/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
      showDebugLink(forgotDebug, forgotDebugInput, data.debug_verify_url);
      forgotStatus.textContent = data.message || "如果该邮箱已注册,重置邮件将发送到您的邮箱。";
      forgotStatus.className = "cfg-status ok";
    } catch (err) {
      forgotStatus.textContent = err.message;
      forgotStatus.className = "cfg-status err";
    } finally {
      btn.disabled = false;
    }
  }

  async function saveProfile() {
    profileEditStatus.textContent = "";
    profileEditStatus.className = "cfg-status";
    profileEditDebug.classList.add("hidden");
    const payload = {
      display_name: profileFieldDisplay.value.trim() || null,
      username: profileFieldUsername.value.trim() || null,
      email: profileFieldEmail.value.trim() || null,
      phone: profileFieldPhone.value.trim() || null,
    };
    const cur = profileFieldCurpass.value;
    const nw = profileFieldNewpass.value;
    const nw2 = profileFieldNewpass2.value;
    if (cur || nw || nw2) {
      if (!cur) {
        profileEditStatus.textContent = "修改密码需要输入当前密码";
        profileEditStatus.className = "cfg-status err";
        return;
      }
      if (nw !== nw2) {
        profileEditStatus.textContent = "两次输入的新密码不一致";
        profileEditStatus.className = "cfg-status err";
        return;
      }
      payload.current_password = cur;
      payload.new_password = nw;
    }
    const btn = document.getElementById("profile-save");
    btn.disabled = true;
    try {
      const res = await fetch("/api/auth/me", {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `保存失败 (${res.status})`);
      profileEditStatus.textContent = data.message || "资料已更新。";
      profileEditStatus.className = "cfg-status ok";
      profileFieldCurpass.value = profileFieldNewpass.value = profileFieldNewpass2.value = "";
      if (data.debug_verify_url) showDebugLink(profileEditDebug, profileEditDebugInput, data.debug_verify_url);
      refreshProfile();
    } catch (err) {
      profileEditStatus.textContent = err.message;
      profileEditStatus.className = "cfg-status err";
    } finally {
      btn.disabled = false;
    }
  }

  async function changeAvatar() {
    if (!window.desktopAPI || !window.desktopAPI.pickImage) return;
    const picked = await window.desktopAPI.pickImage();
    if (!picked) return;
    profileAvatarStatus.textContent = "";
    profileAvatarStatus.className = "cfg-status";
    if (!picked.ok) {
      profileAvatarStatus.textContent = picked.error || "选择图片失败";
      profileAvatarStatus.className = "cfg-status err";
      return;
    }
    if (picked.mime === "application/octet-stream") {
      profileAvatarStatus.textContent = "仅支持 PNG / JPG / WEBP / GIF 图片";
      profileAvatarStatus.className = "cfg-status err";
      return;
    }
    try {
      const bytes = Uint8Array.from(atob(picked.base64), (c) => c.charCodeAt(0));
      const blob = new Blob([bytes], { type: picked.mime });
      const fd = new FormData();
      fd.append("file", blob, picked.name || `avatar.${picked.mime.split("/")[1] || "png"}`);
      const res = await fetch("/api/auth/me/avatar", {
        method: "POST",
        headers: { Authorization: `Bearer ${state.token}` },
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `上传失败 (${res.status})`);
      state.avatar = data.avatar;
      try {
        const cached = JSON.parse(localStorage.getItem(USER_KEY) || "{}");
        localStorage.setItem(USER_KEY, JSON.stringify({ ...cached, avatar: data.avatar }));
      } catch { /* ignore */ }
      profileAvatarStatus.textContent = "头像已更新";
      profileAvatarStatus.className = "cfg-status ok";
      renderAvatar();
      renderHeadAvatar();
    } catch (err) {
      profileAvatarStatus.textContent = err.message;
      profileAvatarStatus.className = "cfg-status err";
    }
  }

  async function login() {
    const username = accountUser.value.trim();
    const password = accountPass.value;
    setAccountStatus("");
    try {
      // Stateless console session (cc_): survives desktop re-logins, unlike the dd_
      // API token /auth/login mints, which the next login on the same channel rotates
      // out — that rotation is what forced a fresh login on every launch. Mirrors the
      // web console's sessionLogin flow.
      const res = await fetch("/api/auth/session-login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || "Invalid username or password");
      }
      const data = await res.json();
      state.token = data.access_token;
      state.username = data.username;
      state.displayName = data.display_name;
      state.roleId = data.role_id;
      state.roleName = data.role_name;
      try {
        localStorage.setItem(USER_KEY, JSON.stringify({
          username: data.username,
          displayName: data.display_name,
          roleId: data.role_id,
          roleName: data.role_name,
        }));
        if (accountRemember.checked) {
          localStorage.setItem(TOKEN_KEY, data.access_token);
          localStorage.setItem(REMEMBER_USER_KEY, data.username);
        } else {
          localStorage.removeItem(TOKEN_KEY);
          localStorage.removeItem(REMEMBER_USER_KEY);
        }
      } catch { /* ignore */ }
      accountPass.value = "";
      closeAccount();
      renderUserMenu();
      refreshProfile();
    } catch (err) {
      setAccountStatus(err.message, "err");
    }
  }

  function logout() {
    state.token = null;
    state.username = null;
    state.displayName = null;
    state.roleId = null;
    state.roleName = null;
    state.quota = null;
    state.sessionId = null;
    state.degradedNoticeShown = false;
    try { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); } catch { /* ignore */ }
    closeProfile();
    renderUserMenu();
  }

  // Signed-out users go straight to the login modal; signed-in users get the menu.
  userMenuTrigger.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (state.token) {
      // Revalidate role / identity against the backend BEFORE showing the menu,
      // so a stale cached roleId can't show the admin group for non-admins.
      await refreshProfile();
      renderUserMenu();
      toggleUserMenu();
    } else {
      openAccount();
    }
  });
  userMenuItems.forEach((item) => {
    item.addEventListener("click", (e) => {
      e.stopPropagation(); // keep the document click handler from closing what we just opened
      handleUserAction(item.dataset.action);
    });
  });
  document.getElementById("settings-close").addEventListener("click", closeSettingsModal);
  settingsOverlay.addEventListener("click", (e) => { if (e.target === settingsOverlay) closeSettingsModal(); });
  document.getElementById("profile-close").addEventListener("click", closeProfile);
  profileOverlay.addEventListener("click", (e) => { if (e.target === profileOverlay) closeProfile(); });
  document.getElementById("account-close").addEventListener("click", closeAccount);
  accountOverlay.addEventListener("click", (e) => { if (e.target === accountOverlay) closeAccount(); });
  document.getElementById("account-register-link").addEventListener("click", openRegister);
  document.getElementById("account-forgot-link").addEventListener("click", openForgot);
  document.getElementById("register-close").addEventListener("click", closeRegister);
  document.getElementById("forgot-close").addEventListener("click", closeForgot);
  registerOverlay.addEventListener("click", (e) => { if (e.target === registerOverlay) closeRegister(); });
  forgotOverlay.addEventListener("click", (e) => { if (e.target === forgotOverlay) closeForgot(); });
  document.getElementById("register-back").addEventListener("click", openAccount);
  document.getElementById("forgot-back").addEventListener("click", openAccount);
  document.getElementById("register-submit").addEventListener("click", register);
  document.getElementById("forgot-submit").addEventListener("click", forgotPassword);
  document.getElementById("register-debug-copy").addEventListener("click", () => copyToClipboard(registerDebugInput.value, registerStatus));
  document.getElementById("forgot-debug-copy").addEventListener("click", () => copyToClipboard(forgotDebugInput.value, forgotStatus));
  document.getElementById("reg-password").addEventListener("keydown", (e) => { if (e.key === "Enter") register(); });
  document.getElementById("reg-password2").addEventListener("keydown", (e) => { if (e.key === "Enter") register(); });
  document.getElementById("forgot-email").addEventListener("keydown", (e) => { if (e.key === "Enter") forgotPassword(); });
  // Password visibility toggle: any .pw-eye button flips its target input password<->text.
  document.addEventListener("click", (e) => {
    const eye = e.target.closest(".pw-eye");
    if (!eye) return;
    const input = document.getElementById(eye.dataset.target);
    if (!input) return;
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    eye.textContent = show ? "🙈" : "👁";
    eye.title = show ? "隐藏密码" : "显示密码";
    input.focus();
  });
  document.addEventListener("click", (e) => {
    if (!userMenu.classList.contains("hidden") && !userMenu.contains(e.target)) closeUserMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeAccount(); closeProfile(); closeUserMenu(); closeSettingsModal(); closeRegister(); closeForgot(); }
  });
  document.getElementById("account-login-btn").addEventListener("click", login);
  accountPass.addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
  document.getElementById("profile-save").addEventListener("click", saveProfile);
  document.getElementById("profile-avatar-change").addEventListener("click", changeAvatar);
  document.getElementById("profile-edit-debug-copy").addEventListener("click", () => copyToClipboard(profileEditDebugInput.value, profileEditStatus));
  profileFieldNewpass.addEventListener("keydown", (e) => { if (e.key === "Enter") saveProfile(); });
  profileFieldNewpass2.addEventListener("keydown", (e) => { if (e.key === "Enter") saveProfile(); });
  themeOptions.forEach((row) => {
    row.addEventListener("click", () => applyTheme(row.dataset.theme));
  });
  if (fontSizeSelect) fontSizeSelect.addEventListener("change", () => applyFontSize(parseInt(fontSizeSelect.value, 10)));
  if (monoSelect) monoSelect.addEventListener("change", () => applyMonoFont(monoSelect.value));
  if (zoomSelect) zoomSelect.addEventListener("change", () => applyZoom(parseFloat(zoomSelect.value)));
  document.querySelectorAll("[data-channel]").forEach((row) => {
    row.addEventListener("click", () => applyChannel(row.dataset.channel));
  });
  rememberBoundsRow.addEventListener("click", () => {
    applyRememberBounds(!rememberBoundsRow.classList.contains("active"));
  });
  settingsTabs.forEach((tab) => {
    const open = () => showSettingsTab(tab.dataset.settingsTab);
    tab.addEventListener("mouseenter", open); // hover switches the panel
    tab.addEventListener("click", open);
  });
  checkUpdatesBtn.addEventListener("click", checkForUpdates);
  updateOpenRelease.addEventListener("click", () => { if (latestReleaseUrl) openUrl(latestReleaseUrl); });
  document.getElementById("feedback-submit").addEventListener("click", submitFeedback);
  // External-link rows inside the settings modal (help / about).
  document.querySelectorAll("#settings-overlay [data-link]").forEach((row) => {
    row.addEventListener("click", () => openUrl(row.dataset.link));
  });
  renderUserMenu();
  refreshProfile();


  document.getElementById("pick-folder").addEventListener("click", pickFolder);
  if (window.desktopAPI.onOpenWorkspace) {
    window.desktopAPI.onOpenWorkspace(pickFolder);
  }
  if (window.desktopAPI.onAddFileToWorkspace) {
    window.desktopAPI.onAddFileToWorkspace(addFileToWorkspace);
  }
  // Help menu quick entries jump straight to the matching settings tab.
  if (window.desktopAPI.onOpenSettings) {
    window.desktopAPI.onOpenSettings((tab) => {
      openSettingsModal();
      showSettingsTab(tab);
    });
  }

  // Restore the last workspace folder so the file tree isn't empty on launch.
  try {
    const saved = localStorage.getItem("deepdive_workspace_dir");
    if (saved) { state.workspaceDir = saved; loadTree(saved); }
  } catch { /* ignore */ }
  reflectWorkspaceName();

  const sidebarToggle = document.getElementById("sidebar-toggle");
  sidebarToggle.addEventListener("click", () => {
    const collapsed = document.getElementById("app").classList.toggle("collapsed");
    sidebarToggle.textContent = collapsed ? "»" : "«";
  });

  // ── Resizable sidebar (drag the splitter) ──
  // Uses pointer events + setPointerCapture so the drag keeps working even when the
  // pointer is released over a <video> surface (whose native controls can swallow
  // window-level mouseup, which would otherwise leave the splitter "stuck" following
  // the mouse). The same pattern is used for the chat drag/resize handles below.
  const splitter = document.getElementById("splitter");
  const sidebarEl = document.getElementById("sidebar");
  splitter.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    document.body.classList.add("resizing");
    const startX = e.clientX;
    const startW = sidebarEl.getBoundingClientRect().width;
    const onMove = (ev) => {
      sidebarEl.style.width = `${Math.min(560, Math.max(200, startW + ev.clientX - startX))}px`;
    };
    const onUp = () => {
      document.body.classList.remove("resizing");
      splitter.removeEventListener("pointermove", onMove);
      splitter.removeEventListener("pointerup", onUp);
      splitter.removeEventListener("pointercancel", onUp);
      try { splitter.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    };
    try { splitter.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    splitter.addEventListener("pointermove", onMove);
    splitter.addEventListener("pointerup", onUp);
    splitter.addEventListener("pointercancel", onUp);
  });

  // ── Chat dock (bottom ↔ right column) ──
  const appEl = document.getElementById("app");
  const chatDock = document.getElementById("chat-dock");
  function syncDock() {
    chatDock.textContent = appEl.classList.contains("chat-right") ? "Dock bottom" : "Dock right";
  }
  chatDock.addEventListener("click", () => {
    chatEl.classList.remove("floating");
    chatFloat.classList.remove("active");
    // Reset drag position AND any resize the user made (the browser writes inline
    // width/height when resizing the floating window), so docking snaps the chat back
    // to its default CSS size instead of keeping a maximized/resized shape.
    chatEl.style.cssText = "";
    appEl.classList.toggle("chat-right");
    syncDock();
    try {
      localStorage.setItem("deepdive_chat_right", appEl.classList.contains("chat-right") ? "1" : "0");
    } catch { /* ignore */ }
  });
  try {
    if (localStorage.getItem("deepdive_chat_right") === "1") appEl.classList.add("chat-right");
  } catch { /* ignore */ }
  syncDock();

  // ── Floating chat window (draggable overlay) ──
  const chatEl = document.getElementById("chat");
  const chatHeader = document.getElementById("chat-header");
  const chatFloat = document.getElementById("chat-float");
  // Remember the pre-float dock side so turning float OFF returns the chat to the same
  // place it was docked before (bottom bar or right column).
  let chatDockSideRight = appEl.classList.contains("chat-right");
  chatFloat.addEventListener("click", () => {
    const floating = chatEl.classList.toggle("floating");
    chatFloat.classList.toggle("active", floating);
    if (floating) {
      chatDockSideRight = appEl.classList.contains("chat-right");
      appEl.classList.remove("chat-right");
      syncDock();
    } else {
      // Turning float OFF docks the chat back to the main window at its default size:
      // clear any drag position and resize the user made while floating (the browser
      // writes inline width/height when resizing), so it snaps back to the pre-float
      // shape instead of keeping the float-adjusted one.
      chatEl.style.cssText = "";
      if (chatDockSideRight) appEl.classList.add("chat-right");
      syncDock();
    }
  });
  chatHeader.addEventListener("pointerdown", (e) => {
    if (!chatEl.classList.contains("floating")) return;
    if (e.target.closest("button")) return;
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const rect = chatEl.getBoundingClientRect();
    const onMove = (ev) => {
      chatEl.style.left = `${rect.left + ev.clientX - startX}px`;
      chatEl.style.top = `${rect.top + ev.clientY - startY}px`;
      chatEl.style.right = "auto";
      chatEl.style.bottom = "auto";
    };
    const onUp = () => {
      chatHeader.removeEventListener("pointermove", onMove);
      chatHeader.removeEventListener("pointerup", onUp);
      chatHeader.removeEventListener("pointercancel", onUp);
      try { chatHeader.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    };
    try { chatHeader.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    chatHeader.addEventListener("pointermove", onMove);
    chatHeader.addEventListener("pointerup", onUp);
    chatHeader.addEventListener("pointercancel", onUp);
  });

  // ── Resize the docked chat by dragging its boundary edge ──
  // Bottom-docked: drag the top edge to change height. Right-docked: drag the left edge to
  // change width. Writes an inline size (clamped), so a later dock still restores the default
  // (docking clears inline styles).
  const chatResize = document.getElementById("chat-resize");
  chatResize.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const right = appEl.classList.contains("chat-right");
    document.body.classList.add("resizing", right ? "resizing-ew" : "resizing-ns");
    const startPos = right ? e.clientX : e.clientY;
    const startSize = right
      ? chatEl.getBoundingClientRect().width
      : chatEl.getBoundingClientRect().height;
    const onMove = (ev) => {
      const delta = (right ? ev.clientX : ev.clientY) - startPos;
      if (right) {
        chatEl.style.width = `${Math.min(560, Math.max(240, startSize - delta))}px`;
      } else {
        chatEl.style.height = `${Math.min(window.innerHeight - 100, Math.max(120, startSize - delta))}px`;
      }
    };
    const onUp = () => {
      document.body.classList.remove("resizing", "resizing-ns", "resizing-ew");
      chatResize.removeEventListener("pointermove", onMove);
      chatResize.removeEventListener("pointerup", onUp);
      chatResize.removeEventListener("pointercancel", onUp);
      try { chatResize.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    };
    try { chatResize.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    chatResize.addEventListener("pointermove", onMove);
    chatResize.addEventListener("pointerup", onUp);
    chatResize.addEventListener("pointercancel", onUp);
  });

  // ── Voice: speak replies + mic input (Web Speech API) ──
  let speakEnabled = false;
  let recognition = null;
  const chatSpeak = document.getElementById("chat-speak");
  const chatMic = document.getElementById("chat-mic");

  function speak(text) {
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    // Match the server-side Kokoro routing: Chinese text → zh, otherwise English.
    u.lang = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/.test(text) ? "zh-CN" : "en-US";
    window.speechSynthesis.speak(u);
  }

  chatSpeak.addEventListener("click", () => {
    speakEnabled = !speakEnabled;
    chatSpeak.classList.toggle("active", speakEnabled);
  });

  chatMic.addEventListener("click", () => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      appendMsg("error", "Voice input is not available in this environment.");
      return;
    }
    if (recognition) {
      recognition.stop();
      return;
    }
    recognition = new SR();
    recognition.lang = "en-US";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.onresult = (e) => {
      const text = e.results[0][0].transcript;
      chatInput.value = text;
      chatForm.dispatchEvent(new Event("submit"));
    };
    recognition.onend = () => { recognition = null; chatMic.classList.remove("active"); };
    recognition.onerror = () => { recognition = null; chatMic.classList.remove("active"); };
    recognition.start();
    chatMic.classList.add("active");
  });

  // ── Sidebar tabs (Files / Sessions) ──
  const tabFiles = document.getElementById("tab-files");
  const tabSessions = document.getElementById("tab-sessions");
  const sessionsEl = document.getElementById("sessions");
  const sessionsList = document.getElementById("sessions-list");
  const fileSearch = document.getElementById("file-search");
  const sessionSearch = document.getElementById("session-search");
  const fileSearchClear = document.getElementById("file-search-clear");
  const sessionSearchClear = document.getElementById("session-search-clear");
  let sessionSearchTimer = null;
  newChatBtn.addEventListener("click", newChat);

  function switchTab(name) {
    const isSessions = name === "sessions";
    tabFiles.classList.toggle("active", !isSessions);
    tabSessions.classList.toggle("active", isSessions);
    treeEl.style.display = isSessions ? "none" : "";
    if (fileSearch) fileSearch.style.display = isSessions ? "none" : "";
    sessionsEl.classList.toggle("hidden", !isSessions);
    if (isSessions) loadSessions();
  }
  tabFiles.addEventListener("click", () => switchTab("files"));
  tabSessions.addEventListener("click", () => switchTab("sessions"));
  if (fileSearch) fileSearch.addEventListener("input", (e) => {
    applyFileSearch(e.target.value);
    if (fileSearchClear) fileSearchClear.classList.toggle("visible", !!e.target.value);
  });
  if (fileSearchClear) fileSearchClear.addEventListener("click", () => {
    fileSearch.value = "";
    applyFileSearch("");
    fileSearchClear.classList.remove("visible");
    fileSearch.focus();
  });
  if (sessionSearch) sessionSearch.addEventListener("input", (e) => {
    state.sessionQuery = e.target.value;
    clearTimeout(sessionSearchTimer);
    sessionSearchTimer = setTimeout(loadSessions, 300);
    if (sessionSearchClear) sessionSearchClear.classList.toggle("visible", !!e.target.value);
  });
  if (sessionSearchClear) sessionSearchClear.addEventListener("click", () => {
    sessionSearch.value = "";
    state.sessionQuery = "";
    clearTimeout(sessionSearchTimer);
    loadSessions();
    sessionSearchClear.classList.remove("visible");
    sessionSearch.focus();
  });

  let sessionSearchSeq = 0;
  async function loadSessions() {
    if (!state.token) {
      state.sessions = [];
      renderSessions();
      return;
    }
    const seq = ++sessionSearchSeq;
    try {
      const q = (state.sessionQuery || "").trim();
      const url = q ? `/api/sessions?q=${encodeURIComponent(q)}` : "/api/sessions";
      const res = await fetch(url, { headers: authHeaders() });
      if (!res.ok) throw new Error(`${res.status}`);
      const data = await res.json();
      if (seq !== sessionSearchSeq) return; // a newer search superseded this one
      state.sessions = data.sessions || [];
      renderSessions();
    } catch {
      if (seq !== sessionSearchSeq) return;
      state.sessions = [];
      renderSessions(true);
    }
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // Wrap every case-insensitive occurrence of q in <mark> (safe innerHTML string).
  function highlightText(text, q) {
    const esc = escapeHtml(text);
    const escQ = escapeHtml(q);
    if (!escQ) return esc;
    const re = new RegExp(`(${escQ.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig");
    return esc.split(re)
      .map((p) => (p.toLowerCase() === escQ.toLowerCase() ? `<mark>${p}</mark>` : p))
      .join("");
  }

  // A short window around the first query match, for the snippet preview.
  function snippetPreview(text, q) {
    const str = String(text);
    const idx = str.toLowerCase().indexOf(String(q).toLowerCase());
    if (idx < 0) return str.slice(0, 120);
    const start = Math.max(0, idx - 40);
    const end = Math.min(str.length, start + 160);
    return (start > 0 ? "…" : "") + str.slice(start, end) + (end < str.length ? "…" : "");
  }

  function renderSessions(failed) {
    sessionsList.innerHTML = "";
    if (failed) {
      sessionsList.innerHTML = '<div class="session-item"><span class="session-summary">Failed to load sessions.</span></div>';
      return;
    }
    if (!state.token) {
      sessionsList.innerHTML = '<div class="session-item"><span class="session-summary">Sign in to view your sessions.</span></div>';
      return;
    }
    const q = (state.sessionQuery || "").trim();
    const list = state.sessions || [];
    if (!list.length) {
      sessionsList.innerHTML = `<div class="session-item"><span class="session-summary">${q ? "No matching sessions." : "No sessions yet."}</span></div>`;
      return;
    }
    for (const s of list) {
      const item = document.createElement("div");
      item.className = "session-item";
      const row = document.createElement("div");
      row.className = "session-row";
      const summary = document.createElement("span");
      summary.className = "session-summary";
      const displayText = s.title || s.summary || s.id.slice(0, 8);
      if (q) summary.innerHTML = highlightText(displayText, q);
      else summary.textContent = displayText;
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "session-edit";
      edit.title = "Rename session";
      edit.textContent = "✎";
      edit.addEventListener("click", (e) => {
        e.stopPropagation();
        startSessionRename(s, summary);
      });
      const del = document.createElement("button");
      del.type = "button";
      del.className = "session-del";
      del.title = "Delete session";
      del.textContent = "🗑";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSession(s.id);
      });
      row.append(summary, edit, del);
      const time = document.createElement("span");
      time.className = "session-time";
      time.textContent = s.created_at ? new Date(s.created_at).toLocaleString() : "";
      item.append(row, time);
      if (q && s.snippet) {
        const snip = document.createElement("div");
        snip.className = "session-snippet";
        snip.innerHTML = highlightText(snippetPreview(s.snippet, q), q);
        item.appendChild(snip);
      }
      item.addEventListener("click", () => resumeSession(s.id));
      sessionsList.appendChild(item);
    }
  }

  async function resumeSession(id) {
    try {
      const res = await fetch(`/api/sessions/${id}`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`${res.status}`);
      const data = await res.json();
      chatLog.innerHTML = "";
      for (const m of data.messages) {
        if (m.role === "tool") continue;
        appendMessage(m.id, m.role === "assistant" ? "assistant" : "user", m.content);
      }
      state.sessionId = id;
      chatTitle.textContent = data.title || (data.messages[0] ? data.messages[0].content.slice(0, 30) : "Chat");
    } catch (err) {
      appendMsg("error", `Failed to load session: ${err.message}`);
    }
  }

  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const message = chatInput.value.trim();
    if (!message) return;
    chatInput.value = "";
    sendChat(message);
  });
})();
