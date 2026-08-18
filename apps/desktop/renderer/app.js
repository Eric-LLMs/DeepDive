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
    quota: null,
    guestId: null,
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
    }
  } catch { /* ignore */ }

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

  const TOKEN_KEY = "deepdive_token";
  const USER_KEY = "deepdive_user";
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
        try {
          localStorage.setItem(USER_KEY, JSON.stringify({
            username: me.username,
            displayName: me.display_name,
            roleId: me.role_id,
            roleName: me.role_name,
          }));
        } catch { /* ignore */ }
        renderUserMenu();
      } else if (res.status === 401) {
        logout();
      }
    } catch { /* backend unreachable; keep cached info */ }
  }

  function renderProfile() {
    profileAvatar.textContent = avatarLetter();
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

  function handleUserAction(action) {
    switch (action) {
      case "profile":
        closeUserMenu();
        if (state.token) openProfile();
        else openAccount();
        break;
      case "web-console": {
        closeUserMenu();
        // Single sign-on: hand the desktop session token to the web console so it
        // auto-signs-in as the current PC user. Without a token it shows its login page.
        const webUrl = state.token
          ? `http://localhost:5173/?sso=${encodeURIComponent(state.token)}`
          : "http://localhost:5173";
        openUrl(webUrl);
        break;
      }
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
      // cloud-drive / check-updates are disabled "Soon" items — no-op.
    }
  }

  function openAccount() {
    setAccountStatus("");
    accountPass.value = "";
    closeUserMenu();
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
        localStorage.setItem(USER_KEY, JSON.stringify({
          username: data.username,
          displayName: data.display_name,
          roleId: data.role_id,
          roleName: data.role_name,
        }));
        if (accountRemember.checked) localStorage.setItem(TOKEN_KEY, data.access_token);
        else localStorage.removeItem(TOKEN_KEY);
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
  const quickSettings = document.getElementById("quick-settings");
  quickSettings.addEventListener("click", (e) => {
    e.stopPropagation();
    openSettingsModal();
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
  document.addEventListener("click", (e) => {
    if (!userMenu.classList.contains("hidden") && !userMenu.contains(e.target)) closeUserMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeAccount(); closeProfile(); closeUserMenu(); closeSettingsModal(); }
  });
  document.getElementById("account-login-btn").addEventListener("click", login);
  accountPass.addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
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
