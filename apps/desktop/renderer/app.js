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
    importedByMsgId: new Map(),  // message id → true when its content is in the query repo (active session)
    sessionImported: false,    // true when every current user message of the session is covered
    sessionImportedLegacy: false, // legacy pre-coverage whole-session import (no per-message data)
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
  // expandPaths: an optional Set of absolute dir paths that must render open (used to
  // reveal a search result by expanding only its ancestor chain, not the whole tree).
  function buildNode(node, open = false, expandPaths = null) {
    if (node.type === "file") {
      const row = document.createElement("div");
      row.className = "tree-node file";
      row.textContent = node.name;
      row.title = node.path;
      row.dataset.path = node.path;
      row.draggable = true;
      row.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", JSON.stringify({ path: node.path, name: node.name }));
        e.dataTransfer.effectAllowed = "move";
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", () => row.classList.remove("dragging"));
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
    row.dataset.path = node.path;
    row.draggable = true;
    row.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", JSON.stringify({ path: node.path, name: node.name }));
      e.dataTransfer.effectAllowed = "move";
      row.classList.add("dragging");
    });
    row.addEventListener("dragend", () => row.classList.remove("dragging"));
    const shouldOpen = open || (expandPaths && expandPaths.has(node.path));
    const toggle = document.createElement("span");
    toggle.className = "tree-toggle";
    toggle.textContent = shouldOpen ? "▾" : "▸";
    row.appendChild(toggle);
    row.appendChild(document.createTextNode(node.name));
    wrapper.appendChild(row);

    const children = document.createElement("div");
    children.className = "tree-children";
    children.style.display = shouldOpen ? "block" : "none";
    if (shouldOpen) {
      for (const child of node.children || []) children.appendChild(buildNode(child, open, expandPaths));
    }
    // ▸/▾ toggle keeps the inline expand/collapse behaviour.
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
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
    // Clicking the folder row browses its contents in the main viewing area.
    row.addEventListener("click", () => browseLocalFolder(node.path));
    wrapper.appendChild(children);
    return wrapper;
  }

  function renderTree(nodes, open = false, expandPaths = null) {
    treeEl.innerHTML = "";
    for (const node of nodes) treeEl.appendChild(buildNode(node, open, expandPaths));
  }

  // Fuzzy filter: keep files/dirs whose name fuzzy-matches the query; a dir is kept
  // when it matches or holds a matching descendant. An empty query restores the tree.
  function filterNodes(nodes, q) {
    const out = [];
    for (const n of nodes) {
      if (n.type === "file") {
        if (fuzzyScore(n.name, q) != null) out.push(n);
      } else {
        const kids = filterNodes(n.children || [], q);
        if (fuzzyScore(n.name, q) != null || kids.length) {
          out.push({ ...n, children: kids });
        }
      }
    }
    return out;
  }

  function applyFileSearch(q) {
    if (!state.treeData) return;
    q = (q || "").trim();
    if (!q) { renderTree(state.treeData); return; }
    renderTree(filterNodes(state.treeData, q), true);
  }

  // ── Local search: fuzzy autocomplete (mirrors the cloud panel) ──
  // Case-insensitive fuzzy score; lower is better, null = no match. Ranked: exact >
  // name prefix > name substring > path hit > loose subsequence of the name.
  function fuzzyScore(hay, q) {
    const h = String(hay || "").toLowerCase();
    const qq = String(q || "").toLowerCase();
    if (!qq) return null;
    if (h === qq) return 0;
    if (h.startsWith(qq)) return 1 + h.length * 0.001;
    const idx = h.indexOf(qq);
    if (idx >= 0) return 2 + idx / h.length;
    let i = 0;
    for (const ch of h) {
      if (ch === qq[i]) i++;
      if (i === qq.length) return 10 + h.length * 0.001;
    }
    return null;
  }

  const fileSuggestEl = document.getElementById("file-search-suggest");
  let localSuggestIndex = -1;

  // Flat list of every file/dir in the workspace (for suggestion scoring).
  function flattenTree(nodes, out = []) {
    for (const n of nodes || []) {
      out.push(n);
      if (n.type === "dir") flattenTree(n.children || [], out);
    }
    return out;
  }

  // Path of a node relative to the workspace root, for the suggestion meta line.
  function relPath(node) {
    if (!state.workspaceDir) return "";
    const root = state.workspaceDir.replace(/\\/g, "/").replace(/\/+$/, "");
    const p = String(node.path).replace(/\\/g, "/");
    const rel = p.startsWith(root + "/") ? p.slice(root.length + 1) : p;
    const i = rel.lastIndexOf("/");
    return i < 0 ? "" : rel.slice(0, i);
  }

  function hideLocalSuggest() {
    if (fileSuggestEl) { fileSuggestEl.classList.add("hidden"); fileSuggestEl.innerHTML = ""; }
    localSuggestIndex = -1;
  }

  function renderLocalSuggest(q) {
    if (!fileSuggestEl) return;
    fileSuggestEl.innerHTML = "";
    if (!q || !state.treeData) { hideLocalSuggest(); return; }
    const scored = [];
    for (const n of flattenTree(state.treeData)) {
      const nameScore = fuzzyScore(n.name, q);
      const pathScore = fuzzyScore(n.path, q);
      const score = nameScore != null ? nameScore : pathScore != null ? 1000 + pathScore : null;
      if (score != null) scored.push({ score, n });
    }
    if (!scored.length) { hideLocalSuggest(); return; }
    scored.sort((a, b) => a.score - b.score);
    for (const { n } of scored.slice(0, 10)) {
      const row = document.createElement("div");
      row.className = "cd-suggest-item";
      const icon = document.createElement("span");
      icon.className = "cd-suggest-icon";
      icon.textContent = n.type === "dir" ? "📁" : "📄";
      const name = document.createElement("span");
      name.className = "cd-suggest-name";
      name.textContent = n.name;
      const meta = document.createElement("span");
      meta.className = "cd-suggest-meta";
      meta.textContent = relPath(n);
      row.append(icon, name, meta);
      row.addEventListener("mousedown", (e) => {
        e.preventDefault();
        if (n.type === "dir") revealLocalDir(n.path); else openLocalSearchHit(n);
      });
      fileSuggestEl.appendChild(row);
    }
    fileSuggestEl.classList.remove("hidden");
  }

  function clearLocalSearch() {
    if (fileSearch) { fileSearch.value = ""; applyFileSearch(""); }
    if (fileSearchClear) fileSearchClear.classList.remove("visible");
    hideLocalSuggest();
  }

  // Clicking a file suggestion opens it in the viewer; a folder suggestion reveals
  // its row in the tree (expands only the ancestor chain) and flashes it.
  function openLocalSearchHit(node) {
    clearLocalSearch();
    Viewer.render(node.path, node.name);
  }

  function revealLocalDir(dirPath) {
    const set = new Set();
    const t = String(dirPath).replace(/\\/g, "/").toLowerCase();
    (function walk(list) {
      for (const n of list) {
        if (n.type !== "dir") continue;
        const np = String(n.path).replace(/\\/g, "/").toLowerCase();
        if (t === np || t.startsWith(np + "/")) {
          set.add(n.path);
          walk(n.children || []);
        }
      }
    })(state.treeData || []);
    clearLocalSearch();
    renderTree(state.treeData || [], false, set);
    const row = treeEl.querySelector(`[data-path="${String(dirPath).replace(/"/g, '\\"')}"]`);
    if (row) {
      row.scrollIntoView({ block: "nearest" });
      row.classList.add("cd-flash");
      setTimeout(() => row.classList.remove("cd-flash"), 1400);
    }
  }

  async function loadTree(dir) {
    const tree = await window.desktopAPI.readTree(dir);
    state.treeData = tree;
    renderTree(tree);
  }

  // ── Folder-browse view (local) ──
  // The main-area listing re-reads the folder's direct children from disk on every
  // navigation, so it always reflects the current filesystem (the sidebar tree is a
  // bounded-depth snapshot that can go stale).
  function relFromAbs(abs) {
    const base = String(state.workspaceDir || "").replace(/[\\/]+$/, "");
    const a = String(abs).replace(/\\/g, "/");
    const b = base.replace(/\\/g, "/");
    return a.startsWith(b + "/") ? a.slice(b.length + 1) : a;
  }

  function absOfRel(rel) {
    if (!rel) return state.workspaceDir;
    return state.workspaceDir.replace(/[\\/]+$/, "") + "\\" + rel.split("/").join("\\");
  }

  async function readLocalDir(relPath) {
    const abs = absOfRel(relPath);
    const raw = await window.desktopAPI.readDir(abs);
    // Dir entries carry the relative path (breadcrumb/navigation); file entries keep
    // the absolute path so the viewer can open them directly.
    const entries = raw.map((e) =>
      e.type === "dir"
        ? { type: "dir", name: e.name, path: relFromAbs(e.path), size: e.size }
        : { type: "file", name: e.name, path: e.path, size: e.size }
    );
    return { entries, localPath: abs };
  }

  async function browseLocalFolder(absPath) {
    if (!state.workspaceDir) return;
    const rel = relFromAbs(absPath);
    try {
      const res = await readLocalDir(rel);
      Viewer.renderFolder(res.entries, {
        rootPath: state.workspaceDir,
        rootName: workspaceDisplayName(state.workspaceDir),
        path: rel,
        read: readLocalDir,
        open: (entry) => Viewer.render(entry.path, entry.name),
        localPath: res.localPath,
      });
    } catch (err) {
      Viewer.toast(`Cannot browse folder: ${err.message || err}`);
    }
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
    const src = document.getElementById("workspace-source");
    if (src) {
      const localOpt = src.querySelector('option[value="local"]');
      if (localOpt) localOpt.textContent = name ? `💻 ${name}` : "💻 Local";
      src.title = state.workspaceDir || "Workspace source";
    }
    if (window.desktopAPI.setWorkspaceLabel) window.desktopAPI.setWorkspaceLabel(name);
  }

  // ── Input modals ──
  // Electron does not implement window.prompt()/window.confirm() (they return null /
  // false silently), so both are replaced by small overlay modals using the app's
  // modal styles. Exposed on window because clouddrive.js (a separate IIFE loaded
  // after this file) reuses them for the cloud note editor.
  window.promptModal = ({ title, placeholder = "", initial = "", multiline = false, okLabel = "Create" } = {}) => {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "overlay";
      const field = multiline
        ? '<textarea class="cd-prompt-input" rows="5"></textarea>'
        : '<input class="cd-prompt-input" type="text" />';
      overlay.innerHTML = `
        <div class="modal cd-prompt-modal">
          <div class="modal-header">
            <h3></h3>
            <button type="button" class="modal-close" title="Cancel">×</button>
          </div>
          <label></label>
          <div class="modal-actions">
            <button type="button" class="cd-prompt-cancel">Cancel</button>
            <button type="button" class="primary cd-prompt-ok"></button>
          </div>
        </div>`;
      overlay.querySelector("h3").textContent = title;
      overlay.querySelector("label").innerHTML = field;
      const input = overlay.querySelector(".cd-prompt-input");
      input.placeholder = placeholder;
      input.value = initial;
      overlay.querySelector(".cd-prompt-ok").textContent = okLabel;
      const done = (val) => { overlay.remove(); resolve(val); };
      overlay.querySelector(".modal-close").addEventListener("click", () => done(null));
      overlay.querySelector(".cd-prompt-cancel").addEventListener("click", () => done(null));
      overlay.querySelector(".cd-prompt-ok").addEventListener("click", () => done(input.value.trim()));
      overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) done(null); });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !multiline) overlay.querySelector(".cd-prompt-ok").click();
        if (e.key === "Escape") { e.stopPropagation(); done(null); }
      });
      document.body.appendChild(overlay);
      input.focus();
      if (!multiline) input.select();
    });
  };

  // Accepts a plain message string or { title, message, okLabel, okClass }.
  window.confirmModal = (arg) => {
    const { title = "Confirm", message, okLabel = "OK", okClass = "primary" } =
      typeof arg === "string" ? { message: arg } : (arg || {});
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "overlay";
      overlay.innerHTML = `
        <div class="modal cd-prompt-modal">
          <div class="modal-header"><h3></h3></div>
          <p class="cd-confirm-msg"></p>
          <div class="modal-actions">
            <button type="button" class="cd-confirm-cancel">Cancel</button>
            <button type="button" class="${okClass} cd-confirm-ok"></button>
          </div>
        </div>`;
      overlay.querySelector("h3").textContent = title;
      overlay.querySelector(".cd-confirm-msg").textContent = message;
      overlay.querySelector(".cd-confirm-ok").textContent = okLabel;
      const done = (val) => { overlay.remove(); resolve(val); };
      overlay.querySelector(".cd-confirm-cancel").addEventListener("click", () => done(false));
      overlay.querySelector(".cd-confirm-ok").addEventListener("click", () => done(true));
      overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) done(false); });
      const okBtn = overlay.querySelector(".cd-confirm-ok");
      okBtn.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { e.stopPropagation(); done(false); }
      });
      document.body.appendChild(overlay);
      okBtn.focus();
    });
  };

  // File menu → "Add File to Workspace": pick a file, copy it into the open workspace,
  // then refresh the tree so it shows up immediately.
  async function addFileToWorkspace() {
    if (!state.workspaceDir) {
      Viewer.toast("Pick a workspace folder first (💻 Local in the sidebar).");
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
    const ok = await window.confirmModal({
      title: "Delete file?", message: `Delete "${name}" from the workspace?\nThis permanently removes the file.`,
      okLabel: "Delete",
    });
    if (!ok) return;
    const res = await window.desktopAPI.deleteFile(filePath, state.workspaceDir);
    if (!res.ok) { Viewer.toast(`Delete failed: ${res.error}`); return; }
    await loadTree(state.workspaceDir);
    Viewer.toast(`Deleted ${res.name}.`);
  }

  // Delete a folder (recursively) from the open workspace. The main process refuses
  // the workspace root itself, so the whole tree can never be wiped by mistake.
  async function deleteLocalFolder(dirPath) {
    if (!state.workspaceDir) return;
    const name = dirPath.replace(/\\/g, "/").split("/").filter(Boolean).pop() || dirPath;
    const ok = await window.confirmModal({
      title: "Delete folder?", message: `Delete "${name}" and everything inside it?\nThis permanently removes the folder from your disk.`,
      okLabel: "Delete",
    });
    if (!ok) return;
    const res = await window.desktopAPI.deleteFolder(dirPath, state.workspaceDir);
    if (!res.ok) { Viewer.toast(`Delete failed: ${res.error}`); return; }
    await loadTree(state.workspaceDir);
    Viewer.toast(`Deleted ${res.name}.`);
  }

  // ── Local tree: right-click / toolbar to create folders + text files ──
  // Mirrors the cloud drive: right-click a folder (or empty space / a file's parent)
  // to create inside it. Local FS writes go through desktopAPI (workspace-bounded).
  function dirnameOf(p) {
    const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
    return i < 0 ? "" : p.slice(0, i);
  }
  let localCtxEl = null;
  // ctx = { parentDir, targetPath?, isDir? } — targetPath is the clicked row's path,
  // isDir whether that row is a folder. Empty area → create-only menu at root.
  function showLocalCtxMenu(x, y, ctx) {
    closeLocalCtxMenu();
    localCtxEl = document.createElement("div");
    localCtxEl.className = "drive-ctxmenu";
    const mk = (label, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.addEventListener("click", () => { closeLocalCtxMenu(); fn(); });
      return b;
    };
    localCtxEl.appendChild(mk("📄 New text file", () => createLocalTextFile(ctx.parentDir)));
    localCtxEl.appendChild(mk("📁 New folder", () => createLocalFolder(ctx.parentDir)));
    if (ctx.targetPath) {
      const sep = document.createElement("div");
      sep.className = "drive-ctxmenu-sep";
      localCtxEl.appendChild(sep);
      if (ctx.isDir) {
        localCtxEl.appendChild(mk("🗑 Delete folder", () => deleteLocalFolder(ctx.targetPath)));
      } else {
        localCtxEl.appendChild(mk("🗑 Delete file", () => deleteFileFromWorkspace(ctx.targetPath)));
      }
    }
    localCtxEl.style.left = `${Math.min(x, window.innerWidth - 200)}px`;
    localCtxEl.style.top = `${Math.min(y, window.innerHeight - 120)}px`;
    document.body.appendChild(localCtxEl);
  }
  function closeLocalCtxMenu() {
    if (localCtxEl) { localCtxEl.remove(); localCtxEl = null; }
  }
  async function createLocalFolder(parentDir) {
    const name = await window.promptModal({ title: "New folder", placeholder: "Folder name", initial: "" });
    if (!name) return;
    const res = await window.desktopAPI.createFolder({ workspaceDir: state.workspaceDir, parentDir, name });
    if (!res.ok) { Viewer.toast(`Create folder failed: ${res.error}`); return; }
    await loadTree(state.workspaceDir);
    Viewer.toast(`Folder "${res.name}" created.`);
  }
  async function createLocalTextFile(parentDir) {
    const name = await window.promptModal({ title: "New text file", placeholder: "File name", initial: "untitled.txt" });
    if (!name) return;
    const content = await window.promptModal({
      title: "Initial content (optional)", placeholder: "Markdown / plain text", initial: "",
      multiline: true, okLabel: "OK",
    });
    if (content === null) return; // cancelled
    const finalName = /\.\w+$/.test(name) ? name : `${name}.txt`;
    const res = await window.desktopAPI.createTextFile({ workspaceDir: state.workspaceDir, parentDir, name: finalName, content });
    if (!res.ok) { Viewer.toast(`Create file failed: ${res.error}`); return; }
    await loadTree(state.workspaceDir);
    Viewer.toast(`Created "${res.name}".`);
  }
  treeEl.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    if (!state.workspaceDir) { Viewer.toast("Pick a workspace folder first (💻 Local in the sidebar)."); return; }
    // Right-click a folder → create inside it / delete it; a file → create in its
    // folder / delete it; empty area → create at the workspace root.
    const nodeRow = e.target.closest(".tree-node");
    const ctx = { parentDir: state.workspaceDir, targetPath: "", isDir: false };
    if (nodeRow && nodeRow.dataset.path) {
      ctx.isDir = nodeRow.classList.contains("dir");
      ctx.targetPath = nodeRow.dataset.path;
      ctx.parentDir = ctx.isDir ? nodeRow.dataset.path : dirnameOf(nodeRow.dataset.path);
    }
    showLocalCtxMenu(e.clientX, e.clientY, ctx);
  });
  document.addEventListener("mousedown", (e) => { if (localCtxEl && !localCtxEl.contains(e.target)) closeLocalCtxMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeLocalCtxMenu(); });
  const localNewFolder = document.getElementById("local-new-folder");
  const localNewText = document.getElementById("local-new-text");
  if (localNewFolder) localNewFolder.addEventListener("click", () => {
    if (!state.workspaceDir) { Viewer.toast("Pick a workspace folder first (💻 Local in the sidebar)."); return; }
    createLocalFolder(state.workspaceDir);
  });
  if (localNewText) localNewText.addEventListener("click", () => {
    if (!state.workspaceDir) { Viewer.toast("Pick a workspace folder first (💻 Local in the sidebar)."); return; }
    createLocalTextFile(state.workspaceDir);
  });

  // ── Local tree: drag-and-drop to move files / folders ──
  // Draggable rows carry their absolute path. Valid drop targets are folder rows
  // (move into them) and empty tree area (move to the workspace root). File rows
  // are not containers. The main process re-parents via fs.renameSync.
  function localDropTargetFor(e) {
    const dirRow = e.target.closest(".tree-node.dir");
    if (dirRow) return { el: dirRow, dest: dirRow.dataset.path || "" };
    if (e.target.closest(".tree-node.file")) return null; // files aren't containers
    return { el: treeEl, dest: state.workspaceDir || "" }; // empty area → workspace root
  }
  function clearLocalDropTargets() {
    treeEl.querySelectorAll(".drop-target").forEach((el) => el.classList.remove("drop-target"));
  }
  treeEl.addEventListener("dragover", (e) => {
    const t = localDropTargetFor(e);
    if (!t || !t.dest) return; // over a file row, or no workspace open
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    clearLocalDropTargets();
    t.el.classList.add("drop-target");
  });
  treeEl.addEventListener("dragleave", (e) => {
    if (!treeEl.contains(e.relatedTarget)) clearLocalDropTargets();
  });
  treeEl.addEventListener("drop", async (e) => {
    e.preventDefault();
    clearLocalDropTargets();
    const t = localDropTargetFor(e);
    if (!t || !t.dest) return;
    let payload = null;
    try { payload = JSON.parse(e.dataTransfer.getData("text/plain") || "null"); } catch { /* ignore */ }
    if (!payload || !payload.path) return;
    const src = payload.path;
    if (src === t.dest) return; // dropped back onto itself
    if (t.dest.startsWith(src + "/") || t.dest.startsWith(src + "\\")) {
      Viewer.toast("Can't move a folder into itself.");
      return;
    }
    const res = await window.desktopAPI.movePath({ workspaceDir: state.workspaceDir, srcPath: src, destDir: t.dest });
    if (!res.ok) { Viewer.toast(`Move failed: ${res.error}`); return; }
    await loadTree(state.workspaceDir);
    Viewer.toast(`Moved "${res.name}".`);
  });
  treeEl.addEventListener("dragend", clearLocalDropTargets);

  // ── Chat ──
  // Markdown rendering for user/assistant bubbles. markdown-it is vendored in
  // renderer/vendor/ (sets window.markdownit). html:false escapes raw HTML (never
  // emitted), and the link validator only allows http/https/mailto + relative/anchor
  // hrefs, so rendered bubbles can't smuggle javascript:/data: payloads. The raw
  // source text is kept separately for copy / read-aloud.
  function isSafeLink(url) {
    const u = String(url || "").trim().toLowerCase();
    if (!u) return false;
    if (u.startsWith("http://") || u.startsWith("https://") || u.startsWith("mailto:")) return true;
    if (u.startsWith("/") || u.startsWith("./") || u.startsWith("../") || u.startsWith("#")) return true;
    return !/^[a-z][a-z0-9+.-]*:/i.test(u); // no scheme → bare relative reference
  }
  const markdownIt = (window.markdownit && window.markdownit({
    html: false,
    linkify: true,
    breaks: true, // soft line breaks → <br>, so the bubble doesn't need white-space: pre-wrap
    validateLink: isSafeLink,
  })) || null;
  // Math support: "$...$" inline and "$$...$$" display formulas render with KaTeX (vendored,
  // sets window.katex; katex.min.css in index.html supplies its fonts/styles). KaTeX output
  // is static HTML — no script execution — and renderToString fails soft (throwOnError:false),
  // so a bad formula degrades to the raw source instead of breaking the bubble.
  function mathPlugin(md) {
    const render = (tex, display) => {
      try {
        const html = window.katex.renderToString(tex, { displayMode: display, throwOnError: false });
        if (html.includes("katex-error")) return escapeHtml(tex); // unparseable → raw source
        return html;
      } catch {
        return escapeHtml(tex); // KaTeX missing or failed → plain & safe
      }
    };
    // Block rule: a "$$ ... $$" or "$ ... $" block on its own line(s). Registered
    // before "lheading" (setext ===/--- headings) so a lone "=" line inside a math
    // block can't be misread as an h1 underline (markdown-it would otherwise turn a
    // "$ ... $" block into a literal "<h1>$...").
    md.block.ruler.before("lheading", "math_block", (state, start, end, silent) => {
      const b = state.bMarks[start] + state.tShift[start];
      const e = state.eMarks[start];
      if (b + 1 > e) return false;
      if (state.src.charCodeAt(b) !== 0x24) return false; // starts with $
      const dbl = state.src[b + 1] === "$";               // $$ block vs $ block
      const open = dbl ? 2 : 1;
      if (b + open > e) return false;
      const close = dbl ? "$$" : "$";
      let content = "", line = start, found = false;
      const first = state.src.slice(b + open, e);
      const tfirst = first.trim();
      if (tfirst.endsWith(close)) { content = tfirst.slice(0, -close.length); found = true; }
      else {
        content = first;
        line++;
        while (line < end) {
          const lb = state.bMarks[line] + state.tShift[line];
          const lt = state.src.slice(lb, state.eMarks[line]).trim();
          if (lt.endsWith(close)) { content += "\n" + lt.slice(0, -close.length); found = true; break; }
          content += "\n" + lt;
          line++;
        }
      }
      if (!found) return false;
      const trimmed = content.trim();
      if (!trimmed.length) return false; // empty "$$$$" is not math
      if (silent) return true;
      state.line = line + 1;
      const token = state.push("math_block", "math", 0);
      token.block = true;
      token.content = trimmed;
      return true;
    });
    // Inline rule: "$$...$$" → display math, "$...$" → inline math. The "$" form rejects
    // spaces right after/before the dollars so "$5"/"$10" stay currency; "$$" never is.
    // Runs after "escape" so "\$" stays a literal dollar sign. Handling "$$" here too lets
    // a display formula glued right after a text line (e.g. "**幂函数**\n$$...$$") still
    // render as a centered block, since the block rule only fires at a block's first line.
    md.inline.ruler.after("escape", "math_inline", (state, silent) => {
      const pos = state.pos, src = state.src;
      if (src.charCodeAt(pos) !== 0x24) return false; // $
      const dbl = src[pos + 1] === "$";
      const close = dbl ? src.indexOf("$$", pos + 2) : src.indexOf("$", pos + 1);
      if (close === -1) return false;
      const content = src.slice(pos + (dbl ? 2 : 1), close);
      if (!content.length || content.includes("\n")) return false;
      if (!dbl && (content[0] === " " || content[content.length - 1] === " ")) return false;
      if (silent) return false;
      state.pos = close + (dbl ? 2 : 1);
      const token = state.push("math_inline", "math", 0);
      token.content = content;
      token.meta = { display: dbl };
      return true;
    });
    md.renderer.rules.math_block = (tokens, idx) => `<div class="math-block">${render(tokens[idx].content, true)}</div>`;
    md.renderer.rules.math_inline = (tokens, idx) => {
      const t = tokens[idx];
      const html = render(t.content, !!(t.meta && t.meta.display));
      return t.meta && t.meta.display
        ? `<span class="math-block">${html}</span>`
        : `<span class="math-inline">${html}</span>`;
    };
  }
  if (markdownIt) markdownIt.use(mathPlugin);
  function renderMarkdown(text) {
    const src = String(text ?? "");
    if (!markdownIt) return escapeHtml(src); // lib missing → keep it plain & safe
    return markdownIt.render(src);
  }

  // Plain message for error/notice rows (no per-message actions).
  function appendMsg(role, text) {
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    div.textContent = text;
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
    return div;
  }

  // Shared action row for user/assistant bubbles. Each button is an icon + text chip so the
  // controls read clearly and aren't easy to fat-finger. Delete opens a short selection mode
  // (startDeleteSelection) where the question and answer are ticked and only the checked ones
  // get removed. getText() returns the text to copy/speak — for streaming bubbles it reads
  // the live buffer, for static ones it closes over the raw markdown source.
  const ACTION_LABELS = {
    edit: "Edit",
    copy: "Copy",
    speak: "Read",
    import: "Import to Knowledge",
    delete: "Delete",
  };
  function buildMsgActions(div, role, getText) {
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    const items = [];
    if (role === "user") items.push(["edit", "✏️", () => startEdit(div)]);
    items.push(
      ["copy", "📋", () => copyText(getText())],
      ["speak", "🔊", () => speakMessage(getText())],
    );
    // Bind a reply to its question and push the Q&A pair into the query repository.
    // The button is born disabled + "✓ Imported" when this pair (or the whole session)
    // is already in the repo — the state is fetched on session load and persisted in
    // `state` so it survives session switches and app restarts.
    if (role === "assistant") {
      // The "✓ Imported" state comes from this message's own `imported_rag` row (returned by
      // GET /sessions/{id}), NOT from the question it happens to bind to — so deleting or
      // re-grouping the question never spreads the state to siblings or hides it. Legacy
      // whole-session imports (no per-message data) stay a blanket ✓.
      const done = state.importedByMsgId.get(div.dataset.id) === true || state.sessionImportedLegacy;
      console.log("[imported] render msg", div.dataset.id, "done", done, "legacy", state.sessionImportedLegacy);
      items.push(["import", "📥", (b) => importPair(div, b), done]);
    }
    items.push(["delete", "🗑", () => startDeleteSelection(div)]);
    for (const [a, glyph, fn, done] of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.a = a;
      b.innerHTML = `<span class="glyph">${glyph}</span><span class="lbl"></span>`;
      b.title = ACTION_LABELS[a];
      b.querySelector(".lbl").textContent = ACTION_LABELS[a];
      if (a === "import" && done) {
        b.disabled = true;
        setBtnLabel(b, "✓ Imported");
      }
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        if (a === "speak") speakMessage(getText(), b);
        else if (a === "import") fn(b); // importPair(div, b): button shows importing / done / failed
        else fn();
      });
      actions.appendChild(b);
    }
    return actions;
  }

  // Update an action button's text + tooltip. The button keeps its glyph; the label
  // reflects import state (Importing… / ✓ Imported / Failed — retry).
  function setBtnLabel(btn, text) {
    if (!btn) return;
    const lbl = btn.querySelector(".lbl");
    if (lbl) lbl.textContent = text;
    else btn.textContent = text;
    btn.title = text;
  }

  // Push one Q&A pair (the preceding user question + this reply) into the query repo.
  // The assistant bubble owns the button; the question is bound by walking up the chat log
  // to the closest user message. Requires a saved session (message ids + session id).
  // The button shows importing → done / failed; while importing it is disabled so the same
  // pair cannot be re-imported (the endpoint is idempotent, but a second run would re-embed).
  async function importPair(div, btn) {
    const asstId = div.dataset.id;
    if (!state.sessionId || !asstId) {
      Viewer.toast("Import needs a saved chat session with message ids.");
      return;
    }
    let userEl = null;
    for (let n = div.previousElementSibling; n; n = n.previousElementSibling) {
      if (n.classList.contains("msg") && n.classList.contains("user") && n.dataset.id) {
        userEl = n;
        break;
      }
    }
    if (!userEl) {
      Viewer.toast("No question found above this reply to bind with.");
      return;
    }
    // Defense-in-depth: a stale render may have left this button enabled even though the
    // pair is already in the repo. Re-check the backend so a pair can never be imported
    // twice, and flip the button to its persistent "✓ Imported" state on the spot.
    const fresh = await fetchImportedState(state.sessionId);
    if (fresh && (fresh.session_imported || fresh.qa_source_ids.includes(userEl.dataset.id))) {
      state.importedByMsgId.set(asstId, true);
      if (userEl && userEl.dataset.id) state.importedByMsgId.set(userEl.dataset.id, true);
      if (fresh.session_imported) state.sessionImported = true;
      if (btn) { btn.disabled = true; setBtnLabel(btn, "✓ Imported"); }
      Viewer.toast("This Q&A pair is already in the query repo.");
      return;
    }
    if (btn) {
      btn.disabled = true;
      setBtnLabel(btn, "Importing…");
    }
    try {
      const res = await fetch("/api/chat/import", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          session_id: state.sessionId,
          user_message_id: userEl.dataset.id,
          assistant_message_id: asstId,
        }),
      });
      if (res.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `${res.status}`);
      }
      const data = await res.json();
      if (btn) setBtnLabel(btn, "✓ Imported");
      // Both halves of the pair are now in the repo — mark them so the button renders ✓ and
      // stays ✓ even if the question it binds to later changes (delete / regroup).
      state.importedByMsgId.set(asstId, true);
      if (userEl && userEl.dataset.id) state.importedByMsgId.set(userEl.dataset.id, true);
      Viewer.toast(`✅ Imported ${data.chunks} chunk${data.chunks === 1 ? "" : "s"} into the query repo.`);
    } catch (err) {
      if (btn) {
        btn.disabled = false; // keep the button clickable so the user can retry
        setBtnLabel(btn, "Failed — retry");
      }
      Viewer.toast(`Import failed: ${err.message}`);
    }
  }

  // Organize the whole active session: the LLM groups its Q&A turns and imports each
  // distinct question (with merged follow-ups) as a query-repo chunk. The job runs
  // async on the worker; this polls GET /jobs/{id} and shows the final result on the
  // button, keeping the button disabled while it is in flight so a session is not
  // queued twice.
  // Import any session's Q&A into the query repo (not just the active one — the sidebar
  // session menu uses this). Active-session UI (chat log import buttons, sessionImported
  // flag) is only touched when the target session is the one on screen.
  //
  // While a job runs the session is marked in `importingSessions`: the sidebar shows an
  // "Importing…" status under its title and locks the row (no resume / no menu) until the
  // job reaches a terminal state.
  const importingSessions = new Set();
  function finishImporting(sessionId) {
    importingSessions.delete(String(sessionId));
    loadSessions();
  }
  async function importSessionFor(sessionId, btn) {
    if (!sessionId) {
      Viewer.toast("No chat session to import.");
      return;
    }
    const isActive = sessionId === state.sessionId;
    // Defense-in-depth mirror of the button's render state: if the backend says the whole
    // session is already imported, don't queue a second organizing job.
    const fresh = await fetchImportedState(sessionId);
    // Modern full coverage blocks a re-queue; a legacy import (no per-message data) is
    // still allowed so re-importing converts it to the per-message model.
    if (fresh && fresh.session_imported && !fresh.legacy_session_imported) {
      if (isActive) state.sessionImported = true;
      if (btn) { btn.disabled = true; setBtnLabel(btn, "✓ Imported"); }
      Viewer.toast("This session is already in the query repo.");
      return;
    }
    if (btn) {
      btn.disabled = true;
      setBtnLabel(btn, "Organizing…");
    }
    let jobId;
    try {
      importingSessions.add(String(sessionId));
      loadSessions();
      const res = await fetch("/api/chat/import-session", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ session_id: sessionId }),
      });
      if (res.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `${res.status}`);
      }
      const data = await res.json();
      jobId = data.job_id;
      Viewer.toast(`⏳ Session queued for Q&A organizing (job ${jobId.slice(0, 8)}…)`);
    } catch (err) {
      finishImporting(sessionId);
      if (btn) {
        btn.disabled = false;
        setBtnLabel(btn, "Failed — retry");
      }
      Viewer.toast(`Import failed: ${err.message}`);
      return;
    }
    // Poll the job to a terminal state (succeeded / failed), capped at 3 minutes.
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2000));
      let st;
      try {
        const r = await fetch(`/api/jobs/${jobId}`, { headers: authHeaders() });
        if (r.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
        st = await r.json();
      } catch (err) {
        finishImporting(sessionId);
        if (btn) {
          btn.disabled = false;
          setBtnLabel(btn, "Import to Knowledge");
        }
        Viewer.toast(`Status check failed: ${err.message}`);
        return;
      }
      if (st.status === "succeeded") {
        const n = (st.result && st.result.chunks) || 0;
        if (btn) { btn.disabled = true; setBtnLabel(btn, "✓ Imported"); }
        if (isActive) {
          // The rebuild re-wrote the whole transcript, so every rendered message is now in the
          // repo: mark them all and reconcile the buttons (a message appended while the job
          // was queued was included too — the rebuild covers the whole current session).
          chatLog.querySelectorAll(".msg.user, .msg.assistant").forEach((el) => {
            if (el.dataset.id) state.importedByMsgId.set(el.dataset.id, true);
          });
          await refreshImportedState();
        }
        finishImporting(sessionId);
        Viewer.toast(`✅ Imported ${n} chunks into the query repo.`);
        return;
      }
      if (st.status === "failed") {
        finishImporting(sessionId);
        if (btn) {
          btn.disabled = false;
          setBtnLabel(btn, "Failed — retry");
        }
        Viewer.toast(`Import failed: ${st.error || "unknown error"}`);
        return;
      }
      // still queued / running → keep polling
    }
    finishImporting(sessionId);
    if (btn) {
      btn.disabled = false;
      setBtnLabel(btn, "Import to Knowledge");
    }
    Viewer.toast("⏳ Still organizing… check the job later or retry.");
  }

  // Shared helper: ask the backend which Q&A pairs of a session are already in the query
  // repo. Returns {qa_source_ids, session_imported} or null on failure, so the render-time
  // refresh and the click-time guard both use the one code path.
  async function fetchImportedState(sessionId) {
    try {
      const res = await fetch(`/api/chat/imported?session_id=${encodeURIComponent(sessionId)}`, {
        headers: authHeaders(),
      });
      if (res.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
      if (!res.ok) { console.log("[imported] non-ok", res.status, sessionId); return null; }
      return await res.json();
    } catch (err) {
      return null; // Non-fatal — leave buttons enabled so import still works.
    }
  }

  // Refresh the aggregate imported state for the active session (whole-session coverage +
  // the legacy pre-coverage blanket flag). Per-message flags come from the session detail
  // (GET /sessions/{id}); this only fills the session-level view used by the import guards.
  // Non-fatal on network errors — the buttons just stay enabled and a retry via the toast
  // still works.
  async function refreshImportedState() {
    state.sessionImported = false;
    state.sessionImportedLegacy = false;
    if (!state.sessionId) {
      return;
    }
    const data = await fetchImportedState(state.sessionId);
    if (!data) return;
    state.sessionImported = !!data.session_imported;
    state.sessionImportedLegacy = !!data.legacy_session_imported;
    console.log("[imported] session", state.sessionId, "sessionImported", state.sessionImported, "legacy", state.sessionImportedLegacy);
    applyImportedStateToDom();
  }

  // Reconcile already-rendered import buttons with the current state. The buttons are
  // born with their state in buildMsgActions, but if state arrives late (or a message
  // was rendered before its id was known) this flips the buttons to the persistent
  // "✓ Imported" state after the fact, so a reopened session can never show an
  // enabled import button on a pair that is already in the repo.
  function applyImportedStateToDom() {
    chatLog.querySelectorAll('.msg.assistant .msg-actions button[data-a="import"]').forEach((b) => {
      const div = b.closest(".msg.assistant");
      const done = (div && state.importedByMsgId.get(div.dataset.id) === true) || state.sessionImportedLegacy;
      if (done) {
        b.disabled = true;
        setBtnLabel(b, "✓ Imported");
      }
    });
  }

  // User/assistant message with the shared action row (buildMsgActions). User bubbles
  // additionally get an edit button (edit + regenerate); assistant bubbles don't —
  // regenerating an assistant message isn't supported by the API, only editing a user
  // question is.
  function appendMessage(id, role, text) {
    if (role !== "user" && role !== "assistant") return appendMsg(role, text);
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    if (id) div.dataset.id = id;
    div.dataset.raw = text; // markdown source, kept for edit + copy of raw text
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.innerHTML = renderMarkdown(text);
    div.appendChild(bubble);
    // Append to the log BEFORE building the action row: buildMsgActions binds an
    // assistant reply to the question above it via previousElementSibling, which only
    // exists once the div is in the DOM. Building first meant the walk always came up
    // empty and an already-imported pair never rendered as "✓ Imported" on reopen.
    chatLog.appendChild(div);
    div.appendChild(buildMsgActions(div, role, () => text));
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
    let text = "";
    const scroll = () => { chatLog.scrollTop = chatLog.scrollHeight; };
    div.appendChild(bubble);
    // Same ordering as appendMessage: the div must be in the log before buildMsgActions
    // so the import button can bind the preceding user question via previousElementSibling.
    chatLog.appendChild(div);
    div.appendChild(buildMsgActions(div, "assistant", () => text));
    return {
      el: div,
      add: (t) => { text += t; bubble.innerHTML = renderMarkdown(text); scroll(); },
      reset: () => { text = ""; bubble.innerHTML = ""; },
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
        headers: authHeaders(),
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

  // ── Delete with selection ──────────────────────────────────────────
  // Delete opens a short selection mode on the message's turn. The question and its answer
  // each get a checkbox (both checked by default) plus a "Delete selected / Cancel" toolbar;
  // only the checked messages are removed (best-effort server DELETE + DOM). Deleting the
  // answer also drops the collapsible thinking bar (.msg-thinking) that produced it, so a
  // removed reply never leaves a stale reasoning block behind.
  let deleteSelection = null;

  function findTurn(msgEl) {
    // #chat-log layout for one turn: [question (.msg.user)] [thinking (.msg-thinking)?] [answer (.msg.assistant)?]
    const isChat = (el) => el && el.classList && (el.classList.contains("user") || el.classList.contains("assistant"));
    const isThinking = (el) => el && el.classList && el.classList.contains("msg-thinking");
    let question = null, thinking = null, answer = null;
    if (msgEl.classList.contains("user")) {
      question = msgEl;
      let cur = msgEl.nextElementSibling;
      while (cur) {
        if (isThinking(cur)) thinking = cur;
        else if (isChat(cur)) { if (cur.classList.contains("assistant")) answer = cur; break; }
        cur = cur.nextElementSibling;
      }
    } else {
      answer = msgEl;
      let cur = msgEl.previousElementSibling;
      while (cur) {
        if (isThinking(cur)) thinking = cur;
        else if (isChat(cur)) { if (cur.classList.contains("user")) question = cur; break; }
        cur = cur.previousElementSibling;
      }
    }
    return { question, thinking, answer };
  }

  function startDeleteSelection(msgEl) {
    if (chatSend.disabled) return; // a chat is in flight — hold off until it finishes
    if (deleteSelection) deleteSelection.cleanup(); // switch target if already selecting
    const { question, thinking, answer } = findTurn(msgEl);
    const group = [question, answer].filter(Boolean);
    if (!group.length) return;

    const bar = document.createElement("div");
    bar.className = "msg-delete-bar";
    const hint = document.createElement("span");
    hint.className = "msg-delete-hint";
    hint.textContent = "Select messages to delete";
    const okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "msg-delete-btn ok";
    okBtn.textContent = "🗑 Delete selected";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "msg-delete-btn cancel";
    cancelBtn.textContent = "✕ Cancel";
    bar.append(hint, okBtn, cancelBtn);
    chatLog.insertBefore(bar, question || msgEl);

    const checks = new Map(); // message element -> { cb }
    for (const el of group) {
      const lab = document.createElement("label");
      lab.className = "msg-delete-check";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true;
      lab.append(cb, document.createTextNode(el.classList.contains("user") ? " Question" : " Answer"));
      el.prepend(lab);
      el.classList.add("selecting");
      const actions = el.querySelector(".msg-actions");
      if (actions) actions.style.display = "none";
      checks.set(el, { cb });
    }

    const cleanup = () => {
      deleteSelection = null;
      bar.remove();
      for (const el of checks.keys()) {
        const lab = el.querySelector(".msg-delete-check");
        if (lab) lab.remove();
        el.classList.remove("selecting");
        const actions = el.querySelector(".msg-actions");
        if (actions) actions.style.display = "";
      }
      document.removeEventListener("keydown", onKey, true);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); cleanup(); }
    };
    document.addEventListener("keydown", onKey, true);

    const confirmDelete = async () => {
      const toRemove = [];
      for (const [el, { cb }] of checks) {
        if (cb.checked) toRemove.push(el);
      }
      // The thinking bar is a per-turn artifact: whenever this turn loses a message, drop it too.
      if (toRemove.length && thinking) toRemove.push(thinking);
      cleanup();
      if (!toRemove.length) return;
      let failed = false;
      for (const el of toRemove) {
        const id = el.dataset.id;
        if (!state.sessionId || !id) continue;
        try {
          const res = await fetch(`/api/sessions/${state.sessionId}/messages/${id}`, {
            method: "DELETE",
            headers: authHeaders(),
          });
          if (res.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
          if (!res.ok) throw new Error(`${res.status}`);
        } catch (err) { failed = true; }
      }
      toRemove.forEach((el) => el.remove());
      if (failed) appendMsg("error", "Some messages couldn't be removed on the server.");
      else if (state.token) loadSessions();
    };

    okBtn.addEventListener("click", (e) => { e.stopPropagation(); confirmDelete(); });
    cancelBtn.addEventListener("click", (e) => { e.stopPropagation(); cleanup(); });
    deleteSelection = { cleanup };
  }

  // ── Edit & regenerate ──────────────────────────────────────────────
  // Editing a user question follows the "edit = re-ask" model used by ChatGPT/Gemini: the
  // edited message and everything after it are removed (server DELETE + DOM), then the
  // question is re-sent through the normal chat stream so a fresh answer regenerates.
  // Implemented entirely client-side — the API has no message-edit endpoint.
  function startEdit(msgEl) {
    if (chatSend.disabled) return; // a chat is in flight — hold off until it finishes
    if (msgEl.querySelector(".msg-editor")) return; // already editing this message
    const bubble = msgEl.querySelector(".msg-bubble");
    const actions = msgEl.querySelector(".msg-actions");
    const original = msgEl.dataset.raw ?? "";

    const editor = document.createElement("div");
    editor.className = "msg-editor";
    const ta = document.createElement("textarea");
    ta.className = "msg-edit-input";
    ta.value = original;
    ta.addEventListener("input", () => {
      ta.style.height = "auto";
      ta.style.height = ta.scrollHeight + "px";
    });
    const bar = document.createElement("div");
    bar.className = "msg-edit-bar";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "msg-edit-btn cancel";
    cancelBtn.textContent = "✕ Cancel";
    const sendBtn = document.createElement("button");
    sendBtn.type = "button";
    sendBtn.className = "msg-edit-btn send";
    sendBtn.textContent = "✓ Send";
    bar.append(cancelBtn, sendBtn);
    editor.append(ta, bar);

    if (actions) actions.style.display = "none";
    bubble.hidden = true;
    bubble.insertAdjacentElement("afterend", editor);

    const cancel = () => {
      editor.remove();
      bubble.hidden = false;
      if (actions) actions.style.display = "";
    };
    const send = () => {
      const text = ta.value.trim();
      if (!text) { ta.focus(); return; }
      resendEdited(msgEl, text);
    };
    cancelBtn.addEventListener("click", cancel);
    sendBtn.addEventListener("click", send);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
      else if (e.key === "Escape") { e.preventDefault(); cancel(); }
    });
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + "px";
  }

  // Drop this message + every later one from the view, best-effort DELETE each on the server,
  // then re-run the chat so a fresh answer streams in for the edited question. The session id
  // is untouched, so the rewritten history stays in the same conversation.
  async function resendEdited(msgEl, text) {
    const toRemove = [];
    let cur = msgEl;
    while (cur) {
      // Message bubbles plus the collapsible thinking bar (msg-thinking) that sits between a
      // question and its answer — both must go so the rewritten history stays contiguous.
      if (cur.classList && (cur.classList.contains("msg") || cur.classList.contains("msg-thinking"))) toRemove.push(cur);
      cur = cur.nextElementSibling;
    }
    let failed = false;
    for (const el of toRemove) {
      const id = el.dataset.id;
      if (!state.sessionId || !id) continue;
      try {
        const res = await fetch(`/api/sessions/${state.sessionId}/messages/${id}`, {
          method: "DELETE",
          headers: authHeaders(),
        });
        if (res.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
        if (!res.ok) throw new Error(`${res.status}`);
      } catch (err) {
        failed = true;
      }
    }
    toRemove.forEach((el) => el.remove());
    if (failed) appendMsg("error", "Some messages couldn't be removed on the server.");
    else if (state.token) loadSessions();
    chatInput.value = "";
    await sendChat(text); // appends the edited question + streams the new answer
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
    if (pendingAttach) {
      payload.attach = pendingAttach;
      pendingAttach = null;
      renderAttachBar();
    }

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
    state.importedByMsgId = new Map();
    state.sessionImported = false;
    state.sessionImportedLegacy = false;
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

  // ── Toolkit generation (workspace file → slides / mind map / summary) ──
  // Sends the selected workspace file (as a workspace-relative path) to the async job
  // endpoint; unlike generateMedia this path carries the auth header. On success a small
  // modal offers "open" actions for each produced artifact.
  const TOOLKIT_LABELS = { slides: "Slides", mindmap: "Mind Map", summary: "Summary" };

  // Default prompts for the 3 toolkit tools, cached from GET /api/toolkit/prompts so the
  // generate dialogs can show them as light-gray placeholders without re-fetching.
  let toolkitDefaultPrompts = null;
  async function fetchToolkitDefaultPrompt(tool) {
    if (toolkitDefaultPrompts === null) {
      toolkitDefaultPrompts = {};
      try {
        const res = await fetch("/api/toolkit/prompts", { headers: authHeaders() });
        if (res.ok) toolkitDefaultPrompts = await res.json();
      } catch { /* fall back to empty defaults */ }
    }
    return toolkitDefaultPrompts[tool] || "";
  }

  // Toolkit generation limits (e.g. the per-file size cap), cached from GET /api/toolkit/config
  // so the generate dialog can warn about oversized files before a job is submitted.
  let toolkitLimits = null;
  async function fetchToolkitLimit() {
    if (toolkitLimits === null) {
      toolkitLimits = {};
      try {
        const res = await fetch("/api/toolkit/config", { headers: authHeaders() });
        if (res.ok) toolkitLimits = await res.json();
      } catch { /* limits stay unknown; the server still guards the size cap */ }
    }
    return toolkitLimits;
  }

  // Compact human-readable byte size (mirrors fmtSize in clouddrive.js).
  function fmtBytes(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  // ── Session → toolkit artifact (chat transcript → Cloud Drive) ──
  // Wires the "Generate Mind Map / Slides / Summary" session-menu entries: pick a Cloud
  // Drive folder (default = drive root), submit the async job, then show an in-list spinner
  // on that session row while it runs (non-blocking — the rest of the app stays usable),
  // and finally list every produced artifact with its location + refresh the drive tree.
  const SESSION_TOOL_ASSETS = {
    mindmap: { label: "Mind Map" },
    slides: { label: "Slides" },
    summary: { label: "Summary" },
  };
  // sessionId -> tool label, for the in-sidebar spinner; mirrors importingSessions.
  const generatingSessions = new Map();
  function finishGenerating(sessionId) {
    generatingSessions.delete(String(sessionId));
    loadSessions();
  }
  // tool -> { jobId, status, result, error } for the LAST submitted toolkit job. Kept so the
  // generate dialog can show the previous submission's status + produced files when reopened,
  // and to disable "Generate" while a job is still running (one job per tool at a time).
  const toolkitJobs = {};
  // tool -> { src, cloudFiles, folderPath, name, prompt } — the dialog's submitted inputs,
  // kept across close/reopen so the window shows exactly what was submitted while the job runs.
  const genDialogState = {};
  // Toolbar "Generate <tool>" buttons, filled when they are bound; the running one pulses.
  const genButtons = {};
  // Tools whose generate dialog is currently open — while open, the in-dialog status box shows
  // the result instead of popping the separate "Saved to Cloud Drive" modal.
  const openGenDialogs = new Set();
  function updateToolbarGenState() {
    for (const tool of ["mindmap", "slides", "summary"]) {
      const btn = genButtons[tool];
      if (!btn) continue;
      const l = toolkitJobs[tool];
      const running = l && (l.status === "queued" || l.status === "running");
      btn.classList.toggle("gen-running", running);
      if (running) {
        // Running: swap to a steady hourglass indicator + label (no blink). The button stays
        // clickable so the user can reopen the dialog and watch the progress / details.
        if (!btn.dataset.idle) btn.dataset.idle = btn.innerHTML;
        btn.innerHTML = `⏳ ${TOOLKIT_LABELS[tool] || tool}`;
        btn.title = `${TOOLKIT_LABELS[tool] || tool} is generating… — click to view progress`;
      } else {
        // Idle / finished: restore the original label and return to a normal button.
        if (btn.dataset.idle) {
          btn.innerHTML = btn.dataset.idle;
          delete btn.dataset.idle;
        }
        btn.title = `Generate a ${TOOLKIT_LABELS[tool] || tool} from this conversation or workspace files`;
      }
    }
  }

  window.generateSessionArtifact = async (tool, s) => {
    if (!state.token) { Viewer.toast("Sign in to generate from a session."); return; }
    const info = SESSION_TOOL_ASSETS[tool] || { label: tool };
    if (generatingSessions.has(String(s.id))) {
      Viewer.toast("Already generating from this session.");
      return;
    }
    let picked;
    try {
      picked = await window.pickDriveFolderModal(`Generate ${info.label} — choose save location`, {
        defaultPrompt: await fetchToolkitDefaultPrompt(tool),
      });
      if (picked === undefined) return; // cancelled
    } catch (err) {
      Viewer.toast(`Could not load cloud folders: ${err.message}`);
      return;
    }
    let jobId;
    try {
      const res = await fetch("/api/toolkit/generate", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          tool,
          session_id: s.id,
          folder_path: picked.folderPath || null,
          prompt: picked.prompt || null,
        }),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      ({ job_id: jobId } = await res.json());
    } catch (err) {
      Viewer.toast(`Generation failed: ${err.message}`);
      return;
    }
    // Job queued: mark the session row and poll in the background — the UI stays usable.
    generatingSessions.set(String(s.id), info.label);
    loadSessions();
    const result = await pollSessionJob(jobId, tool);
    finishGenerating(s.id);
    if (result) showSessionArtifactResult(info, result);
  };

  // Poll the job every 2s with an auth header; resolve the result dict, or null on failure /
  // auth expiry. Generation genuinely takes a while (map-reduce of a large file + the final
  // generate call can exceed 20 minutes; the worker's own timeout is 1h), so keep polling
  // until the job reaches a terminal state instead of giving up early — the toolbar button
  // stays on the hourglass (and Generate stays disabled) until the job really finishes.
  // When ``tool`` is given, the live status is recorded into toolkitJobs[tool] so the
  // generate dialog can reflect the running job when reopened.
  async function pollSessionJob(jobId, tool) {
    const entry = { jobId, status: "queued", result: null, error: null, startedAt: Date.now() };
    if (tool) { toolkitJobs[tool] = entry; updateToolbarGenState(); }
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      let res;
      try {
        res = await fetch(`/api/jobs/${jobId}`, { headers: authHeaders() });
      } catch { continue; }  // transient network hiccup — keep waiting
      if (res.status === 401) {
        Viewer.toast("Session expired; log in again.");
        if (tool) { entry.status = "failed"; entry.error = "Session expired; log in again."; updateToolbarGenState(); }
        return null;
      }
      if (!res.ok) continue;  // transient server error — keep waiting
      let job;
      try { job = await res.json(); } catch { continue; }
      if (tool) { entry.status = job.status; updateToolbarGenState(); }
      if (job.status === "succeeded") {
        const result = job.result || {};
        if (tool) { entry.status = "succeeded"; entry.result = result; updateToolbarGenState(); }
        return result;
      }
      if (job.status === "failed") {
        Viewer.toast(`Generation failed: ${job.error}`);
        if (tool) { entry.status = "failed"; entry.error = job.error; updateToolbarGenState(); }
        return null;
      }
    }
  }

  function showSessionArtifactResult(info, result) {
    if (window.loadCloudDrive) { try { window.loadCloudDrive(); } catch { /* best-effort */ } }
    const assets = (result && result.assets) || [];
    if (!assets.length) {
      Viewer.toast(`${info.label} generated — but no files were reported.`);
      return;
    }
    const overlay = document.createElement("div");
    overlay.className = "overlay";
    const rows = assets.map((a) => {
      const where = a.folder_path ? `in ${a.folder_path}` : "in your Cloud Drive root";
      return `<div style="margin:6px 0"><div class="cd-asset-row">📄 ${escapeHtml(a.name)}</div><div class="cd-hint" style="color:var(--fg-dim);font-size:12px">${escapeHtml(where)}</div></div>`;
    }).join("");
    overlay.innerHTML = `
      <div class="modal cd-prompt-modal">
        <div class="modal-header"><h3></h3></div>
        <p class="cd-confirm-msg"></p>
        ${rows}
        <div class="modal-actions"><button type="button" class="cd-confirm-ok primary">Done</button></div>
      </div>`;
    overlay.querySelector("h3").textContent = `${info.label} generated`;
    overlay.querySelector(".cd-confirm-msg").textContent = "Saved to your Cloud Drive:";
    overlay.querySelector(".cd-confirm-ok").addEventListener("click", () => overlay.remove());
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
  }

  // Submit a session-mode toolkit job; returns the job id (throws on HTTP error).
  async function submitSessionJob(tool, sessionId, prompt, folderPath = null, name = null) {
    const res = await fetch("/api/toolkit/generate", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ tool, session_id: sessionId, folder_path: folderPath, name: name || null, prompt: prompt || null }),
    });
    if (!res.ok) throw new Error(await apiErrorDetail(res));
    return (await res.json()).job_id;
  }

  // Submit a cloud-file-mode toolkit job; returns the job id (throws on HTTP error).
  async function submitCloudFilesJob(tool, fileIds, prompt, folderPath = null, name = null) {
    const res = await fetch("/api/toolkit/generate", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ tool, file_ids: fileIds, folder_path: folderPath, name: name || null, prompt: prompt || null }),
    });
    if (!res.ok) throw new Error(await apiErrorDetail(res));
    return (await res.json()).job_id;
  }

  // Extract FastAPI's {"detail": "..."} error body so a rejected request (e.g. an oversized
  // file refused at enqueue) shows a readable reason instead of just "400 Bad Request".
  async function apiErrorDetail(res) {
    try {
      const b = await res.json();
      if (b && b.detail) return String(b.detail);
    } catch { /* response body is not JSON */ }
    return `${res.status} ${res.statusText}`;
  }

  // Open the Cloud Drive view at the folder where a generated artifact landed (the "view
  // output" link in the success status). "" opens the drive root.
  function openOutputInDrive(folderPath) {
    const src = document.getElementById("workspace-source");
    if (src && src.value !== "cloud") src.value = "cloud";
    setCloudMode(true);
    const tabFiles = document.getElementById("tab-files");
    if (tabFiles && !tabFiles.classList.contains("active")) tabFiles.click();
    if (window.openCloudFolder) window.openCloudFolder(folderPath || "");
  }

  // ── Toolbar generate: one clean dialog (session or cloud files + output folder + prompt) ──
  // Picking a tool opens a single modal: choose "this conversation" or Cloud Drive files, pick
  // an output folder (default = Cloud Drive root), drop in an optional prompt, then Generate /
  // Cancel. Local files are not a source — upload them to your Cloud Drive first. The job runs
  // in the background (toast), so the modal closes right away and the rest of the app stays usable.
  async function openGenerateDialog(tool) {
    const label = TOOLKIT_LABELS[tool] || tool;
    const hasSession = !!state.sessionId;
    const defaultPrompt = await fetchToolkitDefaultPrompt(tool);
    const saved = genDialogState[tool] || {};
    const limits = await fetchToolkitLimit();
    const maxBytes = limits.max_file_bytes || 0;

    const overlay = document.createElement("div");
    overlay.className = "overlay";
    const modal = document.createElement("div");
    modal.className = "modal cd-gen-modal";

    const header = document.createElement("div");
    header.className = "modal-header";
    const h = document.createElement("h3");
    h.textContent = `Generate ${label}`;
    const close = document.createElement("button");
    close.type = "button"; close.className = "modal-close"; close.innerHTML = "&times;"; close.title = "Cancel";
    header.append(h, close);

    const body = document.createElement("div");
    body.className = "cd-gen-body";

    // Source: session or cloud files — mutually exclusive, one section visible at a time.
    const srcLabel = document.createElement("div");
    srcLabel.className = "cd-gen-label";
    srcLabel.textContent = "Source";
    const srcRow = document.createElement("div");
    srcRow.className = "cd-gen-sources";
    const sessionBtn = document.createElement("button");
    sessionBtn.type = "button";
    sessionBtn.className = "cd-src-option";
    sessionBtn.innerHTML = hasSession
      ? "<b>💬 This conversation</b><span>Generate a " + label + " from the current chat</span>"
      : "<b>💬 This conversation</b><span>Send a message first, then come back</span>";
    sessionBtn.disabled = !hasSession;
    const filesBtn = document.createElement("button");
    filesBtn.type = "button";
    filesBtn.className = "cd-src-option";
    filesBtn.innerHTML = "<b>☁️ Cloud files</b><span>Generate from files in your Cloud Drive</span>";
    srcRow.append(sessionBtn, filesBtn);
    body.append(srcLabel, srcRow);

    // Cloud file selection (shown only in "files" mode).
    const fileSection = document.createElement("div");
    fileSection.className = "cd-gen-files hidden";
    const filesLabel = document.createElement("div");
    filesLabel.className = "cd-gen-label";
    filesLabel.textContent = "Selected files";
    const chips = document.createElement("div");
    chips.className = "cd-gen-chips";
    const addRow = document.createElement("div");
    addRow.className = "cd-gen-adds";
    const addCloud = document.createElement("button");
    addCloud.type = "button";
    addCloud.className = "cd-gen-add";
    addCloud.textContent = "＋ Add cloud file";
    addRow.append(addCloud);
    // Warning shown when a selected file exceeds the per-file size cap for generation.
    const sizeWarn = document.createElement("div");
    sizeWarn.className = "cd-gen-size-warn hidden";
    fileSection.append(filesLabel, chips, sizeWarn, addRow);
    body.append(fileSection);

    // Output folder — both modes save to the Cloud Drive (default = drive root).
    const dirLabel = document.createElement("div");
    dirLabel.className = "cd-gen-label";
    dirLabel.textContent = "Output folder";
    const dirRow = document.createElement("div");
    dirRow.className = "cd-gen-dir";
    const dirVal = document.createElement("span");
    dirVal.className = "cd-gen-dir-val";
    const dirBtn = document.createElement("button");
    dirBtn.type = "button";
    dirBtn.className = "cd-gen-add";
    dirBtn.textContent = "Choose folder…";
    dirRow.append(dirVal, dirBtn);
    body.append(dirLabel, dirRow);

    // Output file name — optional; blank auto-names from the session title / first file.
    const nameLabel = document.createElement("div");
    nameLabel.className = "cd-gen-label";
    nameLabel.textContent = "File name (optional)";
    const nameInput = document.createElement("input");
    nameInput.className = "cd-gen-name";
    nameInput.type = "text";
    nameInput.spellcheck = false;
    const nameHint = document.createElement("div");
    nameHint.className = "cd-gen-name-hint";
    body.append(nameLabel, nameInput, nameHint);

    const promptLabel = document.createElement("div");
    promptLabel.className = "cd-gen-label";
    promptLabel.textContent = "Prompt";
    const promptEl = document.createElement("textarea");
    promptEl.className = "cd-gen-prompt";
    promptEl.rows = 5;
    promptEl.spellcheck = false;
    promptEl.placeholder = defaultPrompt || "Optional: write your own requirements here…";
    body.append(promptLabel, promptEl);

    // Restore the previous submission's inputs so the window shows exactly what was submitted.
    nameInput.value = saved.name || "";
    promptEl.value = saved.prompt || "";

    // Status of the previous submission for this tool (running / succeeded files / failure).
    const statusBox = document.createElement("div");
    statusBox.className = "cd-gen-status hidden";
    body.append(statusBox);

    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "cd-gen-reset";
    resetBtn.textContent = "Reset";
    resetBtn.title = "Clear this window back to the initial state";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "cd-gen-cancel";
    cancelBtn.textContent = "Cancel";
    const okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "primary cd-gen-ok";
    okBtn.textContent = "Generate";
    okBtn.disabled = true;
    actions.append(resetBtn, cancelBtn, okBtn);

    modal.append(header, body, actions);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    openGenDialogs.add(tool);

    // Default to the session when one exists; "files" is the fallback. A saved state from a
    // previous open (kept across close) restores exactly what was submitted.
    let src = saved.src || (hasSession ? "session" : "files");
    let folderPath = saved.folderPath ?? null;   // null = Cloud Drive root
    let sessionTitle = "";   // filled async from GET /sessions/{id} when a session is active
    const cloudFiles = (saved.cloudFiles || []).map((f) => ({ ...f }));   // { id, name }

    // Mirror the worker's sanitize_name + artifact_plan so the "default name" hint tells the
    // user exactly which files will land in the Cloud Drive.
    function sanitizeLike(title) {
      let s = (title || "").replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").replace(/\s+/g, " ").trim();
      if (!s) return "session";
      return s.length > 80 ? s.slice(0, 80) : s;
    }
    // Mirrors Path.stem: drop the last extension so "chapter.pdf" → "chapter" (the output name
    // must not embed ".pdf" in the middle, e.g. chapter_mindmap.mmd).
    function stemLike(name) {
      const i = (name || "").lastIndexOf(".");
      return i > 0 ? name.slice(0, i) : name;
    }
    function defaultArtifactNames(toolName, title) {
      const safe = sanitizeLike(title);
      if (toolName === "mindmap") return [`${safe}_mindmap.mmd`];
      if (toolName === "summary") return [`${safe}_summary.md`];
      return [`${safe}_slides.md`, `${safe}_slides.pptx`];
    }

    function refresh() {
      chips.innerHTML = "";
      const empty = cloudFiles.length === 0;
      // No selected files → hide the whole list (label + chips) so no empty box shows.
      filesLabel.classList.toggle("hidden", empty);
      chips.classList.toggle("hidden", empty);
      const over = maxBytes > 0 ? cloudFiles.filter((f) => (f.size || 0) > maxBytes) : [];
      for (const f of cloudFiles) {
        const chip = document.createElement("span");
        chip.className = "cd-gen-chip" + (over.includes(f) ? " cd-gen-chip-over" : "");
        chip.textContent = f.name + (f.size != null ? `  ·  ${fmtBytes(f.size)}` : "");
        const x = document.createElement("button");
        x.type = "button";
        x.className = "cd-gen-chip-x";
        x.textContent = "×";
        x.title = "Remove";
        x.addEventListener("click", () => {
          cloudFiles.splice(cloudFiles.indexOf(f), 1);
          refresh();
        });
        chip.appendChild(x);
        chips.appendChild(chip);
      }
      // Turn the list into a fixed ~4-row scroll region only once it would actually
      // overflow — a handful of files render as plain chips with no surrounding box.
      if (!empty) {
        const total = [...chips.children].reduce((sum, c) => sum + c.offsetHeight + 6, -6);
        chips.classList.toggle("cd-gen-chips-overflow", total > 136);
      } else {
        chips.classList.remove("cd-gen-chips-overflow");
      }
      sizeWarn.classList.toggle("hidden", over.length === 0);
      if (over.length) {
        const maxMb = Math.round(maxBytes / (1024 * 1024));
        sizeWarn.innerHTML = `⚠️ Too large for generation (max ${maxMb}MB): ${over.map((f) => escapeHtml(f.name)).join(", ")} — remove ${over.length > 1 ? "them" : "it"} to continue.`;
      }
      fileSection.classList.toggle("hidden", src !== "files");
      sessionBtn.classList.toggle("cd-active", src === "session");
      filesBtn.classList.toggle("cd-active", src === "files");
      dirVal.textContent = folderPath
        ? `☁️ My Drive / ${folderPath.split("/").join(" / ")}`
        : "☁️ Cloud Drive (root)";
      const pending = (() => {
        const l = toolkitJobs[tool];
        return l && (l.status === "queued" || l.status === "running");
      })();
      okBtn.disabled = pending || (src === "session" ? !hasSession : cloudFiles.length === 0) || over.length > 0;
      resetBtn.disabled = pending;   // do not clear state mid-run
      renderStatus();

      const currentTitle = src === "session" ? sessionTitle : stemLike(cloudFiles[0]?.name || "");
      const typed = nameInput.value.trim();
      const baseTitle = typed || currentTitle;
      const names = defaultArtifactNames(tool, baseTitle || "session");
      nameHint.textContent = (typed ? "Will create: " : "Default: ") + names.join(" / ");
      nameInput.placeholder = defaultArtifactNames(tool, currentTitle || "session")[0].replace(/\.\w+$/, "");
    }

    // Show the previous submission's status for this tool; while a job is still running the
    // Generate button stays disabled so two runs of the same tool can't overlap.
    function renderStatus() {
      const last = toolkitJobs[tool];
      if (!last) { statusBox.classList.add("hidden"); return; }
      statusBox.classList.remove("hidden");
      if (last.status === "queued" || last.status === "running") {
        const secs = last.startedAt ? Math.max(1, Math.round((Date.now() - last.startedAt) / 1000)) : 0;
        statusBox.className = "cd-gen-status";
        statusBox.textContent = `⏳ ${label} is generating… (${secs}s) — you can close this dialog; it continues in the background, reopen anytime.`;
      } else if (last.status === "succeeded") {
        const assets = (last.result && last.result.assets) || [];
        statusBox.className = "cd-gen-status cd-gen-status-ok";
        if (assets.length) {
          const rows = assets.map((a) =>
            `📄 ${escapeHtml(a.name)} <span class="cd-gen-status-where">${escapeHtml(a.folder_path ? "in " + a.folder_path : "in Cloud Drive root")}</span> <a href="#" class="cd-gen-open" data-folder="${escapeHtml(a.folder_path || "")}">view output</a>`
          ).join("<br>");
          statusBox.innerHTML = `✓ ${label} generated:<br>${rows}`;
        } else {
          statusBox.textContent = `✓ ${label} generated — no files were reported.`;
        }
      } else {
        statusBox.className = "cd-gen-status cd-gen-status-err";
        statusBox.textContent = `✗ ${label} failed: ${last.error || "unknown error"}`;
      }
    }

    // While the dialog is open and a job for this tool is still running, poll it so the status
    // box updates live and the Generate button re-enables the moment it finishes.
    let statusTimer = null;
    function startStatusPolling() {
      stopStatusPolling();
      const last = toolkitJobs[tool];
      if (!last || (last.status !== "queued" && last.status !== "running")) return;
      statusTimer = setInterval(async () => {
        const cur = toolkitJobs[tool];
        if (!cur) { stopStatusPolling(); return; }
        let job;
        try {
          const res = await fetch(`/api/jobs/${cur.jobId}`, { headers: authHeaders() });
          if (!res.ok) { stopStatusPolling(); return; }
          job = await res.json();
        } catch { return; }
        cur.status = job.status;
        updateToolbarGenState();
        if (job.status === "succeeded") { cur.result = job.result || {}; cur.status = "succeeded"; stopStatusPolling(); }
        else if (job.status === "failed") { cur.error = job.error; cur.status = "failed"; stopStatusPolling(); }
        refresh();
      }, 2000);
    }
    function stopStatusPolling() {
      if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
    }

    sessionBtn.addEventListener("click", () => { src = "session"; refresh(); });
    filesBtn.addEventListener("click", () => { src = "files"; refresh(); });
    nameInput.addEventListener("input", refresh);

    addCloud.addEventListener("click", async () => {
      const picked = await window.pickCloudFiles({ title: `Add cloud files — ${label}`, okLabel: "Add" });
      for (const f of picked) if (!cloudFiles.some((x) => x.id === f.id)) cloudFiles.push(f);
      if (picked.length) src = "files";
      refresh();
    });

    dirBtn.addEventListener("click", async () => {
      const picked = await window.pickDriveFolderModal("Choose output folder", { prompt: false, okLabel: "Select" });
      if (picked === undefined) return; // cancelled
      folderPath = picked.folderPath || null;
      refresh();
    });

    // Keep whatever was submitted/filled so reopening the dialog restores it while a job runs.
    function saveDialogState() {
      genDialogState[tool] = {
        src,
        cloudFiles: cloudFiles.map((f) => ({ ...f })),
        folderPath,
        name: nameInput.value,
        prompt: promptEl.value,
      };
    }

    function finish() {
      stopStatusPolling();
      saveDialogState();
      openGenDialogs.delete(tool);
      document.removeEventListener("keydown", onEsc);
      overlay.remove();
    }
    function onEsc(e) {
      if (e.key !== "Escape") return;
      const overlays = document.querySelectorAll(".overlay");
      if (overlays.length && overlays[overlays.length - 1] !== overlay) return; // a child picker is open
      finish();
    }
    // One-click clear: back to the initial state — drop the selected files / folder / name /
    // prompt and forget the previous job + result (toolbar button returns to normal).
    resetBtn.addEventListener("click", () => {
      cloudFiles.length = 0;
      folderPath = null;
      nameInput.value = "";
      promptEl.value = "";
      src = hasSession ? "session" : "files";
      delete genDialogState[tool];
      delete toolkitJobs[tool];
      updateToolbarGenState();
      stopStatusPolling();
      refresh();
    });
    // "view output" link in the success status → open the Cloud Drive at that folder.
    statusBox.addEventListener("click", (e) => {
      const a = e.target.closest(".cd-gen-open");
      if (!a) return;
      e.preventDefault();
      openOutputInDrive(a.dataset.folder || "");
    });
    cancelBtn.addEventListener("click", () => finish());
    close.addEventListener("click", () => finish());
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) finish(); });
    document.addEventListener("keydown", onEsc);

    okBtn.addEventListener("click", () => {
      const prompt = promptEl.value.trim() || null;
      const name = nameInput.value.trim() || null;
      const info = SESSION_TOOL_ASSETS[tool] || { label };
      saveDialogState();
      Viewer.toast(`Generating ${label}…`);
      // Keep the dialog open: inputs stay as submitted, Generate greys out, the status box
      // shows progress. Close/reopen restores this exact state; Generate releases on done.
      okBtn.disabled = true;
      (async () => {
        try {
          const jobId = src === "session"
            ? await submitSessionJob(tool, state.sessionId, prompt, folderPath, name)
            : await submitCloudFilesJob(tool, cloudFiles.map((f) => f.id), prompt, folderPath, name);
          // Record the job now so the open dialog's status box + toolbar show "running" and the
          // dialog's own poll loop drives the live progress; pollSessionJob keeps the background
          // completion path (result modal / failure toast) alive too.
          toolkitJobs[tool] = { jobId, status: "queued", result: null, error: null, startedAt: Date.now() };
          updateToolbarGenState();
          refresh();
          startStatusPolling();
          const result = await pollSessionJob(jobId, tool);
          if (result && !openGenDialogs.has(tool)) showSessionArtifactResult(info, result);
        } catch (err) {
          Viewer.toast(`Generation failed: ${err.message}`);
          toolkitJobs[tool] = { jobId: null, status: "failed", result: null, error: err.message, startedAt: Date.now() };
          updateToolbarGenState();
          refresh();
        }
      })();
    });

    // Fetch the session title so the default-name hint is accurate in session mode.
    if (hasSession) {
      fetch(`/api/sessions/${state.sessionId}`, { headers: authHeaders() })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { if (d && d.title) { sessionTitle = d.title; refresh(); } })
        .catch(() => {});
    }

    refresh();
    startStatusPolling();
  }


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
        ? `http://localhost:5273/?sso=${encodeURIComponent(session)}${hash}`
        : `http://localhost:5273/${hash}`
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


  // Workspace source dropdown (💻 Local / ☁️ Cloud). The Local option doubles as the
  // folder picker: selecting it opens the workspace folder chooser when none is open.
  // clouddrive.js registers window.loadCloudDrive and reads window.__cloudDriveActive;
  // this module owns both so the local tree and the cloud panel never fight.
  window.__cloudDriveActive = false;
  function setCloudMode(active) {
    window.__cloudDriveActive = active;
    treeEl.style.display = active ? "none" : "";
    // Resolve the search box here (not the later const) so the startup restore of a
    // saved cloud source works before the sidebar-tabs block declares fileSearch.
    const search = document.getElementById("file-search");
    if (search) search.style.display = active ? "none" : "";
    const cloudEl = document.getElementById("clouddrive");
    if (cloudEl) cloudEl.classList.toggle("hidden", !active);
    const localCreate = document.getElementById("local-create");
    if (localCreate) localCreate.style.display = active ? "none" : "";
    if (active && window.loadCloudDrive) window.loadCloudDrive();
  }
  const workspaceSource = document.getElementById("workspace-source");
  if (workspaceSource) {
    workspaceSource.addEventListener("change", () => {
      try { localStorage.setItem("deepdive_workspace_source", workspaceSource.value); } catch { /* ignore */ }
      if (workspaceSource.value === "cloud") {
        setCloudMode(true);
      } else {
        setCloudMode(false);
        if (!state.workspaceDir) pickFolder(); // Local = pick the workspace folder
      }
      // Reset the main viewer so it never keeps showing content from the other
      // source (e.g. a cloud folder view lingering after switching back to Local).
      Viewer.close();
      // Pull the sidebar back to the Files tab so the chosen source is what's shown.
      const tabFiles = document.getElementById("tab-files");
      if (tabFiles && !tabFiles.classList.contains("active")) tabFiles.click();
    });
  }
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

  // Restore the last source choice (cloud vs local); clouddrive.js runs after app.js
  // and refreshes the panel itself when it sees #clouddrive already visible.
  try {
    if (workspaceSource && localStorage.getItem("deepdive_workspace_source") === "cloud") {
      workspaceSource.value = "cloud";
      setCloudMode(true);
    }
  } catch { /* ignore */ }

  const sidebarToggle = document.getElementById("sidebar-toggle");
  // The splitter drag writes an inline width on #sidebar; an inline style beats the
  // `#app.collapsed #sidebar { width: 44px }` rule, so a dragged sidebar would collapse
  // to content-hidden-but-still-wide. Remember the dragged width and clear the inline
  // style on collapse so the 44px strip applies; restore it on expand.
  let sidebarDragWidth = null;
  sidebarToggle.addEventListener("click", () => {
    const collapsed = document.getElementById("app").classList.toggle("collapsed");
    sidebarToggle.textContent = collapsed ? "»" : "«";
    if (collapsed) {
      if (sidebarEl.style.width) sidebarDragWidth = sidebarEl.style.width;
      sidebarEl.style.width = "";
    } else if (sidebarDragWidth) {
      sidebarEl.style.width = sidebarDragWidth;
    }
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

  // ── Hide chat → floating DeepDive logo mini-icon ──
  // Hiding just adds a class (display:none) so the chat keeps its dock side / floating
  // position / drag offset; restoring removes the class, putting it back exactly where
  // it was. The mini icon is a fixed corner button showing the DeepDive logo.
  const chatHide = document.getElementById("chat-hide");
  const chatMini = document.getElementById("chat-mini");
  chatHide.addEventListener("click", () => {
    chatEl.classList.add("minimized");
    chatMini.classList.remove("hidden");
  });
  chatMini.addEventListener("click", () => {
    chatEl.classList.remove("minimized");
    chatMini.classList.add("hidden");
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

  // ── Chat attachments: attach the current file / selected text / a window screenshot ──
  // The in-input 📎 button was removed (per request); the toolbar's "attach to chat"
  // entry point and the pending-attach chip still work.
  const chatAttachBar = document.getElementById("chat-attach-bar");
  let pendingAttach = null; // { kind: "asset", asset_id, name } riding on the next send

  function setPendingAttach(attach) {
    pendingAttach = attach || null;
    renderAttachBar();
  }

  function renderAttachBar() {
    chatAttachBar.innerHTML = "";
    chatAttachBar.classList.toggle("hidden", !pendingAttach);
    if (!pendingAttach) return;
    const chip = document.createElement("span");
    chip.className = "chat-attach-chip";
    const label = document.createElement("span");
    label.textContent = `🔗 ${pendingAttach.name || "attached file"}`;
    const rm = document.createElement("button");
    rm.textContent = "✕";
    rm.title = "Remove attachment";
    rm.addEventListener("click", () => setPendingAttach(null));
    chip.appendChild(label);
    chip.appendChild(rm);
    chatAttachBar.appendChild(chip);
  }

  async function sha256Hex(bytes) {
    const buf = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  // Upload raw bytes into the cloud drive (the same init → chunked PUT → complete flow the
  // cloud panel uses) and return the created file row { id, name }.
  async function uploadBytesToCloud(bytes, name) {
    const hex = await sha256Hex(bytes);
    const init = await fetch("/api/files/init-upload", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ sha256: hex, size: bytes.length, name, folder_path: null, mime_type: null }),
    });
    if (init.status === 401) { openAccount(); throw new Error("Session expired — sign in again."); }
    if (!init.ok) throw new Error(`Upload init failed (${init.status})`);
    const body = await init.json();
    let created;
    if (body.status !== "instant") {
      const chunkSize = body.chunk_size || 5 * 1024 * 1024;
      const num = body.num_chunks || Math.ceil(bytes.length / chunkSize);
      for (let i = 0; i < num; i++) {
        const start = i * chunkSize;
        const slice = bytes.slice(start, Math.min(bytes.length, start + chunkSize));
        const put = await fetch(`/api/files/${body.asset_id}/chunks/${i}`, {
          method: "PUT",
          headers: { Authorization: `Bearer ${state.token}` },
          body: new Blob([slice]),
        });
        if (!put.ok) throw new Error(`chunk ${i} failed (${put.status})`);
      }
      await fetch(`/api/files/${body.asset_id}/complete`, { method: "POST", headers: authHeaders() });
      created = await (await fetch(`/api/files/${body.asset_id}`, { headers: authHeaders() })).json();
    } else {
      created = body.asset || await (await fetch(`/api/files/${body.asset_id}`, { headers: authHeaders() })).json();
    }
    return created;
  }

  // The attach action shared by the viewer toolbar (🔗 / 📷) and the chat 📎 button.
  // mode: "file" (attach the currently-open file) | "screenshot" (window capture).
  async function attachCurrent(mode, file) {
    if (!state.token) {
      appendMsg("error", "Sign in to attach files to the chat.");
      openAccount();
      return;
    }
    try {
      let created;
      if (mode === "screenshot") {
        const shot = await window.desktopAPI.captureWindow();
        if (!shot || !shot.ok) { appendMsg("error", (shot && shot.error) || "Screenshot failed."); return; }
        const b64 = String(shot.data).replace(/^data:image\/png;base64,/, "");
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        created = await uploadBytesToCloud(bytes, `screenshot_${Date.now()}.png`);
      } else {
        // A cloud asset opened in the viewer attaches directly by id; a local file is
        // uploaded to the cloud drive first (the agent works on drive assets).
        const cf = window.__cloudDriveActive === true ? (window.__getViewerCloudFile?.() || null) : null;
        if (cf && cf.id) {
          setPendingAttach({ kind: "asset", asset_id: cf.id, name: cf.name });
          return;
        }
        const f = file || {};
        if (!f.path) { appendMsg("error", "No file is open to attach."); return; }
        const res = await window.desktopAPI.readFileBytes(f.path);
        if (!res.ok) throw new Error(res.error || "Cannot read the file");
        created = await uploadBytesToCloud(res.data, f.name || "attached.bin");
      }
      setPendingAttach({ kind: "asset", asset_id: created.id, name: created.name });
    } catch (err) {
      appendMsg("error", `Attach failed: ${err.message || err}`);
    }
  }

  Viewer.setAttachHandler(attachCurrent);

  // The chat input ＋ button: open an OS file picker, upload the chosen file to the
  // cloud drive, and stage it as the pending attachment (rides on the next send).
  const chatAttachPick = document.getElementById("chat-attach-pick");
  chatAttachPick.addEventListener("click", async () => {
    if (!state.token) { appendMsg("error", "Sign in to attach files to the chat."); openAccount(); return; }
    const path = await window.desktopAPI.pickFile();
    if (!path) return; // user canceled the dialog
    const name = String(path).split(/[\\/]/).pop() || "attached.bin";
    try {
      const res = await window.desktopAPI.readFileBytes(path);
      if (!res.ok) throw new Error(res.error || "Cannot read the file");
      const created = await uploadBytesToCloud(res.data, name);
      setPendingAttach({ kind: "asset", asset_id: created.id, name: created.name });
    } catch (err) {
      appendMsg("error", `Attach failed: ${err.message || err}`);
    }
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
    // The Files tab shows either the local tree or the cloud drive, depending on the
    // workspace-source dropdown (clouddrive.js writes window.__cloudDriveActive). The
    // cloud panel must also be hidden on the Sessions tab, like the tree + search are.
    const cloudActive = window.__cloudDriveActive === true;
    const showLocal = !isSessions && !cloudActive;
    treeEl.style.display = showLocal ? "" : "none";
    if (fileSearch) fileSearch.style.display = showLocal ? "" : "none";
    if (!showLocal) hideLocalSuggest();
    const cloudEl = document.getElementById("clouddrive");
    if (cloudEl) cloudEl.classList.toggle("hidden", isSessions || !cloudActive);
    const localCreate = document.getElementById("local-create");
    if (localCreate) localCreate.style.display = showLocal ? "" : "none";
    sessionsEl.classList.toggle("hidden", !isSessions);
    if (isSessions) loadSessions();
  }
  tabFiles.addEventListener("click", () => switchTab("files"));
  tabSessions.addEventListener("click", () => switchTab("sessions"));
  if (fileSearch) fileSearch.addEventListener("input", (e) => {
    applyFileSearch(e.target.value);
    renderLocalSuggest(e.target.value.trim());
    if (fileSearchClear) fileSearchClear.classList.toggle("visible", !!e.target.value);
  });
  if (fileSearch) fileSearch.addEventListener("keydown", (e) => {
    const items = fileSuggestEl ? fileSuggestEl.querySelectorAll(".cd-suggest-item") : [];
    if (e.key === "Escape") {
      e.preventDefault();
      clearLocalSearch();
      fileSearch.blur();
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!items.length) return;
      const dir = e.key === "ArrowDown" ? 1 : -1;
      localSuggestIndex = (localSuggestIndex + dir + items.length) % items.length;
      items.forEach((el, i) => el.classList.toggle("active", i === localSuggestIndex));
      items[localSuggestIndex].scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (!items.length) return;
      items[localSuggestIndex >= 0 ? localSuggestIndex : 0].dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    }
  });
  if (fileSearchClear) fileSearchClear.addEventListener("click", () => {
    clearLocalSearch();
    fileSearch.focus();
  });
  document.addEventListener("mousedown", (e) => {
    if (fileSuggestEl && !fileSuggestEl.classList.contains("hidden")
        && fileSearch && !fileSearch.closest(".search-wrap").contains(e.target)) {
      hideLocalSuggest();
    }
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

  // ── Session row menu (Gemini-style ⋯): Pin / Rename / Import to Knowledge / Delete ──
  // Stroke-style (line) icons, no fills — matched to the rest of the chrome.
  const SESSION_ICONS = {
    more: '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>',
    pin: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z"/></svg>',
    pencil: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>',
    book: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
    mindmap: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v5"/><path d="M12 8l4 4"/><path d="M12 8l-4 4"/><circle cx="6" cy="16" r="2"/><circle cx="12" cy="16" r="2"/><circle cx="18" cy="16" r="2"/></svg>',
    slides: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="11" rx="1"/><path d="M12 15v4"/><path d="M8 20l4-3 4 3"/></svg>',
    notes: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6"/><path d="M9 17h4"/></svg>',
    trash: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  };

  // Pin state is client-side only (Gemini-style): a pinned session sorts above the rest.
  function getPinned() {
    try { return JSON.parse(localStorage.getItem("deepdive_pinned") || "[]"); } catch { return []; }
  }
  function setPinned(list) {
    try { localStorage.setItem("deepdive_pinned", JSON.stringify(list)); } catch { /* ignore */ }
  }
  function isPinned(id) { return getPinned().includes(String(id)); }
  function togglePin(id) {
    const list = getPinned();
    const i = list.indexOf(String(id));
    if (i >= 0) list.splice(i, 1);
    else list.push(String(id));
    setPinned(list);
  }

  // One shared dropdown, positioned under the ⋯ button that opened it.
  let sessionMenuEl = null;
  function ensureSessionMenu() {
    if (sessionMenuEl) return sessionMenuEl;
    sessionMenuEl = document.createElement("div");
    sessionMenuEl.className = "session-menu";
    document.body.appendChild(sessionMenuEl);
    return sessionMenuEl;
  }
  function closeSessionMenu() {
    if (sessionMenuEl) {
      sessionMenuEl.classList.remove("open");
      sessionMenuEl.innerHTML = "";
    }
  }
  function openSessionMenu(anchor, items) {
    const menu = ensureSessionMenu();
    menu.innerHTML = "";
    for (const it of items) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "session-menu-item" + (it.danger ? " danger" : "");
      row.innerHTML = `${it.iconSvg}<span>${it.label}</span>`;
      row.addEventListener("click", () => {
        closeSessionMenu();
        it.action();
      });
      menu.appendChild(row);
    }
    menu.classList.add("open");
    const r = anchor.getBoundingClientRect();
    const mw = menu.offsetWidth || 200;
    const mh = menu.offsetHeight || items.length * 34 + 8;
    let left = r.right + 4;
    if (left + mw > window.innerWidth - 8) left = r.left - mw - 4;
    if (left < 8) left = 8;
    let top = r.bottom + 4;
    if (top + mh > window.innerHeight - 8) top = r.top - mh - 4;
    if (top < 8) top = 8;
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
  }
  document.addEventListener("click", (e) => {
    if (sessionMenuEl && sessionMenuEl.classList.contains("open") && !sessionMenuEl.contains(e.target)) closeSessionMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSessionMenu();
  });

  function buildSessionMenu(s, summaryEl) {
    const pinned = isPinned(s.id);
    return [
      {
        iconSvg: SESSION_ICONS.pin,
        label: pinned ? "Unpin" : "Pin",
        action: () => { togglePin(s.id); loadSessions(); },
      },
      {
        iconSvg: SESSION_ICONS.pencil,
        label: "Rename",
        action: () => startSessionRename(s, summaryEl),
      },
      {
        iconSvg: SESSION_ICONS.book,
        label: "Import to Knowledge",
        action: () => importSessionFor(s.id),
      },
      {
        iconSvg: SESSION_ICONS.mindmap,
        label: "Generate Mind Map",
        action: () => generateSessionArtifact("mindmap", s),
      },
      {
        iconSvg: SESSION_ICONS.slides,
        label: "Generate Slides",
        action: () => generateSessionArtifact("slides", s),
      },
      {
        iconSvg: SESSION_ICONS.notes,
        label: "Summarize & Save Notes",
        action: () => generateSessionArtifact("summary", s),
      },
      {
        iconSvg: SESSION_ICONS.trash,
        label: "Delete",
        danger: true,
        action: () => confirmDeleteSession(s),
      },
    ];
  }

  // Gemini-style delete confirmation: Cancel / Delete.
  async function confirmDeleteSession(s) {
    const ok = await window.confirmModal({
      title: "Delete chat?",
      message: "This will delete the prompts and responses in this chat session, plus any content you created.",
      okLabel: "Delete",
      okClass: "danger",
    });
    if (!ok) return;
    await deleteSession(s.id);
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
    const pinnedIds = getPinned();
    const sorted = [...list].sort((a, b) => {
      const pa = pinnedIds.includes(String(a.id)) ? 0 : 1;
      const pb = pinnedIds.includes(String(b.id)) ? 0 : 1;
      return pa - pb;
    });
    for (const s of sorted) {
      const item = document.createElement("div");
      item.className = "session-item";
      const importing = importingSessions.has(String(s.id));
      const genLabel = generatingSessions.get(String(s.id));
      if (importing) item.classList.add("session-importing");
      else if (genLabel) item.classList.add("session-generating");
      const row = document.createElement("div");
      row.className = "session-row";
      const summary = document.createElement("span");
      summary.className = "session-summary";
      const displayText = s.title || s.summary || s.id.slice(0, 8);
      if (q) summary.innerHTML = highlightText(displayText, q);
      else summary.textContent = displayText;
      const more = document.createElement("button");
      more.type = "button";
      more.className = "session-more";
      more.title = "Session actions";
      more.innerHTML = SESSION_ICONS.more;
      more.disabled = importing || genLabel;
      more.addEventListener("click", (e) => {
        e.stopPropagation();
        openSessionMenu(more, buildSessionMenu(s, summary));
      });
      if (isPinned(s.id)) {
        const pin = document.createElement("span");
        pin.className = "session-pinned";
        pin.title = "Pinned";
        pin.innerHTML = SESSION_ICONS.pin;
        row.append(pin, summary, more);
      } else {
        row.append(summary, more);
      }
      const time = document.createElement("span");
      if (importing) {
        time.className = "session-import-status";
        time.textContent = "⏳ Importing…";
      } else if (genLabel) {
        time.className = "session-gen-status";
        const spin = document.createElement("span");
        spin.className = "session-gen-spinner";
        time.append(spin, document.createTextNode(` Generating ${genLabel}…`));
      } else {
        time.className = "session-time";
        time.textContent = s.created_at ? new Date(s.created_at).toLocaleString() : "";
      }
      item.append(row, time);
      if (q && s.snippet) {
        const snip = document.createElement("div");
        snip.className = "session-snippet";
        snip.innerHTML = highlightText(snippetPreview(s.snippet, q), q);
        item.appendChild(snip);
      }
      item.addEventListener("click", () => {
        if (importing) return; // locked while the import job is running
        if (genLabel) return; // locked while an artifact job is running
        resumeSession(s.id);
      });
      sessionsList.appendChild(item);
    }
  }

  async function resumeSession(id) {
    try {
      const res = await fetch(`/api/sessions/${id}`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`${res.status}`);
      const data = await res.json();
      state.sessionId = id;
      // Per-message import state comes straight from the rows returned by GET /sessions/{id}:
      // the backend flips imported_rag on import, so a reopened session shows "✓ Imported"
      // without any re-derivation. Rebuild the map before rendering.
      state.importedByMsgId = new Map();
      for (const m of data.messages) {
        if (m.imported_rag) state.importedByMsgId.set(m.id, true);
      }
      // Fetch legacy whole-session blanket state (pre-flag imports) BEFORE rendering so the
      // import buttons are born with their persistent state (disabled + "✓ Imported").
      await refreshImportedState();
      chatLog.innerHTML = "";
      for (const m of data.messages) {
        if (m.role === "tool") continue;
        appendMessage(m.id, m.role === "assistant" ? "assistant" : "user", m.content);
      }
      chatTitle.textContent = data.title || (data.messages[0] ? data.messages[0].content.slice(0, 30) : "Chat");
      // Reconciliation: the buttons were rendered from state above, but re-apply so a
      // pair already in the repo can never show as importable even if a message was
      // rendered without its bound question id.
      applyImportedStateToDom();
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

  // Enter sends the message; Shift+Enter inserts a newline. A <textarea> does not submit on
  // Enter the way a text <input> inside a form did, so forward the key to the form.
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      chatForm.requestSubmit();
    }
  });

  // ── Toolbar: generate summary / mindmap / slides from the current conversation ──
  const chatGenTools = [
    ["mindmap", "chat-gen-mindmap"],
    ["slides", "chat-gen-slides"],
    ["summary", "chat-gen-summary"],
  ];
  for (const [tool, id] of chatGenTools) {
    const btn = document.getElementById(id);
    if (!btn) continue;
    genButtons[tool] = btn;
    btn.addEventListener("click", () => openGenerateDialog(tool));
  }
  updateToolbarGenState();
})();
