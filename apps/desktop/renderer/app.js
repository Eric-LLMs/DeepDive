// App wiring: folder picker + file tree, viewer dispatch, and the chat pane.
(() => {
  const treeEl = document.getElementById("tree");
  const chatLog = document.getElementById("chat-log");
  const chatForm = document.getElementById("chat-form");
  const chatInput = document.getElementById("chat-input");
  const chatSend = document.getElementById("chat-send");

  const state = {
    sessionId: null,
    token: null,
    username: null,
    displayName: null,
    roleId: null,
    roleName: null,
    guestId: null,
  };
  try { state.token = localStorage.getItem("deepdive_token"); } catch { /* ignore */ }
  try { state.guestId = localStorage.getItem("deepdive_guest_id"); } catch { /* ignore */ }

  // ── File tree ──
  function buildNode(node) {
    if (node.type === "file") {
      const row = document.createElement("div");
      row.className = "tree-node file";
      row.textContent = node.name;
      row.title = node.path;
      row.addEventListener("click", () => Viewer.render(node.path, node.name));
      return row;
    }
    const wrapper = document.createElement("div");
    const row = document.createElement("div");
    row.className = "tree-node dir";
    const toggle = document.createElement("span");
    toggle.className = "tree-toggle";
    toggle.textContent = "▸";
    row.appendChild(toggle);
    row.appendChild(document.createTextNode(node.name));
    wrapper.appendChild(row);

    const children = document.createElement("div");
    children.className = "tree-children";
    children.style.display = "none";
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

  async function pickFolder() {
    const dir = await window.desktopAPI.pickFolder();
    if (!dir) return;
    const tree = await window.desktopAPI.readTree(dir);
    treeEl.innerHTML = "";
    for (const node of tree) treeEl.appendChild(buildNode(node));
  }

  // ── Chat ──
  function appendMsg(role, text) {
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    div.textContent = text;
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  function authHeaders() {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    return headers;
  }

  async function sendChat(message) {
    appendMsg("user", message);
    chatSend.disabled = true;
    try {
      const payload = { message, session_id: state.sessionId ?? undefined };
      if (!state.token) payload.user_id = state.guestId ?? undefined;
      const res = await fetch("/api/chat", {
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
      const data = await res.json();
      state.sessionId = data.session_id ?? state.sessionId;
      if (!state.token && data.user_id) {
        state.guestId = data.user_id;
        try { localStorage.setItem("deepdive_guest_id", data.user_id); } catch { /* ignore */ }
      }
      appendMsg("assistant", data.answer);
      if (speakEnabled) speak(data.answer);
    } catch (err) {
      appendMsg("error", `Request failed: ${err.message}`);
    } finally {
      chatSend.disabled = false;
    }
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

  // ── Settings dropdown (list) + login modal ──
  const settingsPanel = document.getElementById("settings-panel");
  const accountOverlay = document.getElementById("account-overlay");
  const settingsAccountBtn = document.getElementById("settings-account-btn");
  const settingsAccountValue = document.getElementById("settings-account-value");
  const settingsAccountInfo = document.getElementById("settings-account-info");
  const accountUser = document.getElementById("account-user");
  const accountPass = document.getElementById("account-pass");
  const accountStatus = document.getElementById("account-status");
  const accountWho = document.getElementById("account-who");
  const accountTheme = document.getElementById("account-theme");
  const accountRemember = document.getElementById("account-remember");

  const TOKEN_KEY = "deepdive_token";
  const THEME_KEY = "deepdive_theme";

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    accountTheme.value = theme;
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* ignore */ }
  }
  try { applyTheme(localStorage.getItem(THEME_KEY) || "dark"); } catch { /* ignore */ }

  function setAccountStatus(text, cls) {
    accountStatus.textContent = text;
    accountStatus.className = "cfg-status" + (cls ? " " + cls : "");
  }

  function renderAccountInfo() {
    accountWho.innerHTML = "";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = state.displayName || state.username || "Signed in";
    accountWho.appendChild(name);
    if (state.roleName) {
      const role = document.createElement("span");
      role.className = "tier" + (state.roleName === "vip" || state.roleName === "admin" ? " vip" : "");
      role.textContent = state.roleName;
      accountWho.appendChild(role);
    }
  }

  function closeSettings() {
    settingsPanel.classList.add("hidden");
  }
  function closeAccount() {
    accountOverlay.classList.add("hidden");
  }

  function renderSettingsAccount() {
    const loggedIn = !!state.token;
    settingsAccountValue.textContent = loggedIn
      ? (state.displayName || state.username || "Signed in")
      : "Sign in";
    if (loggedIn) {
      renderAccountInfo();
    } else {
      settingsAccountInfo.classList.add("hidden");
    }
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
        renderSettingsAccount();
      } else if (res.status === 401) {
        logout();
      }
    } catch { /* backend unreachable; keep cached info */ }
  }

  function toggleSettings() {
    if (settingsPanel.classList.contains("hidden")) {
      renderSettingsAccount();
      settingsPanel.classList.remove("hidden");
      if (state.token) refreshProfile();
    } else {
      settingsPanel.classList.add("hidden");
    }
  }

  function openAccount() {
    setAccountStatus("");
    accountPass.value = "";
    accountOverlay.classList.remove("hidden");
  }

  async function login() {
    const username = accountUser.value.trim();
    const password = accountPass.value;
    setAccountStatus("");
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) throw new Error("Invalid username or password");
      const data = await res.json();
      state.token = data.access_token;
      state.username = data.username;
      state.displayName = data.display_name;
      state.roleId = data.role_id;
      state.roleName = data.role_name;
      try {
        if (accountRemember.checked) localStorage.setItem(TOKEN_KEY, data.access_token);
        else localStorage.removeItem(TOKEN_KEY);
      } catch { /* ignore */ }
      accountPass.value = "";
      closeAccount();
      closeSettings();
      renderSettingsAccount();
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
    state.sessionId = null;
    try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ }
    renderSettingsAccount();
  }

  document.getElementById("open-settings").addEventListener("click", toggleSettings);
  settingsAccountBtn.addEventListener("click", () => {
    if (state.token) {
      settingsAccountInfo.classList.toggle("hidden");
      if (!settingsAccountInfo.classList.contains("hidden")) renderAccountInfo();
    } else {
      openAccount();
    }
  });
  document.getElementById("account-close").addEventListener("click", closeAccount);
  accountOverlay.addEventListener("click", (e) => { if (e.target === accountOverlay) closeAccount(); });
  document.addEventListener("click", (e) => {
    if (!settingsPanel.classList.contains("hidden")
        && !settingsPanel.contains(e.target)
        && e.target.id !== "open-settings") {
      closeSettings();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeAccount(); closeSettings(); }
  });
  document.getElementById("account-login-btn").addEventListener("click", login);
  accountPass.addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
  document.getElementById("account-logout").addEventListener("click", logout);
  renderSettingsAccount();
  document.getElementById("account-help").addEventListener("click", () => {
    const url = "https://github.com/Eric-LLMs/DeepDive";
    if (window.desktopAPI && window.desktopAPI.openExternal) {
      window.desktopAPI.openExternal(url);
    } else {
      window.open(url, "_blank");
    }
  });
  accountTheme.addEventListener("change", () => applyTheme(accountTheme.value));

  document.getElementById("pick-folder").addEventListener("click", pickFolder);

  const sidebarToggle = document.getElementById("sidebar-toggle");
  sidebarToggle.addEventListener("click", () => {
    const collapsed = document.getElementById("app").classList.toggle("collapsed");
    sidebarToggle.textContent = collapsed ? "»" : "«";
  });

  // ── Resizable sidebar (drag the splitter) ──
  const splitter = document.getElementById("splitter");
  const sidebarEl = document.getElementById("sidebar");
  splitter.addEventListener("mousedown", (e) => {
    e.preventDefault();
    document.body.classList.add("resizing");
    const startX = e.clientX;
    const startW = sidebarEl.getBoundingClientRect().width;
    function onMove(ev) {
      sidebarEl.style.width = `${Math.min(560, Math.max(200, startW + ev.clientX - startX))}px`;
    }
    function onUp() {
      document.body.classList.remove("resizing");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
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
    chatEl.style.left = chatEl.style.top = chatEl.style.right = chatEl.style.bottom = "";
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
  chatFloat.addEventListener("click", () => {
    const floating = chatEl.classList.toggle("floating");
    chatFloat.classList.toggle("active", floating);
    if (floating) {
      appEl.classList.remove("chat-right");
      syncDock();
    }
  });
  chatHeader.addEventListener("mousedown", (e) => {
    if (!chatEl.classList.contains("floating")) return;
    if (e.target.closest("button")) return;
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const rect = chatEl.getBoundingClientRect();
    function onMove(ev) {
      chatEl.style.left = `${rect.left + ev.clientX - startX}px`;
      chatEl.style.top = `${rect.top + ev.clientY - startY}px`;
      chatEl.style.right = "auto";
      chatEl.style.bottom = "auto";
    }
    function onUp() {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
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
    u.lang = "en-US";
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

  function switchTab(name) {
    const isSessions = name === "sessions";
    tabFiles.classList.toggle("active", !isSessions);
    tabSessions.classList.toggle("active", isSessions);
    treeEl.style.display = isSessions ? "none" : "";
    sessionsEl.classList.toggle("hidden", !isSessions);
    if (isSessions) loadSessions();
  }
  tabFiles.addEventListener("click", () => switchTab("files"));
  tabSessions.addEventListener("click", () => switchTab("sessions"));

  async function loadSessions() {
    if (!state.token) {
      sessionsEl.innerHTML = '<div class="session-item"><span class="session-summary">Sign in to view your sessions.</span></div>';
      return;
    }
    try {
      const res = await fetch("/api/sessions", { headers: authHeaders() });
      if (!res.ok) throw new Error(`${res.status}`);
      const data = await res.json();
      sessionsEl.innerHTML = "";
      if (!data.sessions.length) {
        sessionsEl.innerHTML = '<div class="session-item"><span class="session-summary">No sessions yet.</span></div>';
        return;
      }
      for (const s of data.sessions) {
        const item = document.createElement("div");
        item.className = "session-item";
        const summary = document.createElement("span");
        summary.className = "session-summary";
        summary.textContent = s.summary || s.id.slice(0, 8);
        const time = document.createElement("span");
        time.className = "session-time";
        time.textContent = s.created_at ? new Date(s.created_at).toLocaleString() : "";
        item.append(summary, time);
        item.addEventListener("click", () => resumeSession(s.id));
        sessionsEl.appendChild(item);
      }
    } catch {
      sessionsEl.innerHTML = '<div class="session-item"><span class="session-summary">Failed to load sessions.</span></div>';
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
        appendMsg(m.role === "assistant" ? "assistant" : "user", m.content);
      }
      state.sessionId = id;
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
