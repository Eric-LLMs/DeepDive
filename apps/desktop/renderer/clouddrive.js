// Cloud drive browser + markdown note editor for the desktop sidebar.
//
// The server is the single source of truth: this panel calls the same REST API the
// web console uses, so edits here show up in the web console on refresh and vice
// versa. The main process proxies /api/* to the backend, and auth reuses the session
// token app.js stores in localStorage["deepdive_token"] — this module is a separate
// IIFE so it reads that token directly instead of reaching into app.js's private scope.
//
// Scope is intentionally a subset of the web console: My Drive files + folders only.
// Trash / workspaces / sharing stay in the web console.
(() => {
  const TOKEN_KEY = "deepdive_token";

  const cloudEl = document.getElementById("clouddrive");
  const cdPathEl = document.getElementById("cd-path");
  const cdListEl = document.getElementById("cd-list");
  const cdStatusEl = document.getElementById("cd-status");
  const cdNewFolder = document.getElementById("cd-new-folder");
  const cdNewText = document.getElementById("cd-new-text");
  const cdSearch = document.getElementById("cd-search");
  const cdSearchClear = document.getElementById("cd-search-clear");
  const cdSuggest = document.getElementById("cd-suggest");
  const noteEditor = document.getElementById("note-editor");
  const noteTitleEl = document.getElementById("note-title");
  const noteTextarea = document.getElementById("note-textarea");
  const notePreviewPane = document.getElementById("note-preview-pane");
  const noteModeEl = document.getElementById("note-mode");
  const notePreviewBtn = document.getElementById("note-preview");
  const noteSaveBtn = document.getElementById("note-save");
  const noteCloseBtn = document.getElementById("note-close");

  // The Local/Cloud source switch lives in app.js (the workspace dropdown at the top
  // of the sidebar). It owns window.__cloudDriveActive and toggles this panel's
  // visibility; this module only renders cloud content on demand.

  // My Drive is rendered as an inline-expanding tree: drive.expanded holds the set of
  // folder paths whose contents are shown below them (▸/▾), so clicking a folder never
  // navigates away — you always see the subdirectories beneath it.
  const drive = { expanded: new Set(), files: [], folders: [] };
  const note = { asset: null, dirty: false, preview: false };

  // ── Auth + fetch ──
  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
  }

  async function apiFetch(path, init = {}) {
    const headers = { ...(init.headers || {}) };
    const t = getToken();
    if (t) headers.Authorization = `Bearer ${t}`;
    if (init.body && typeof init.body === "string") headers["Content-Type"] = "application/json";
    const res = await fetch(`/api${path}`, { ...init, headers });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try { const b = await res.json(); if (b && b.detail) detail = b.detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    return res.json();
  }

  // app.js (the workspace dropdown) calls this when entering cloud mode so the list
  // reloads from the server; it also toggles this panel's .hidden class itself.
  window.loadCloudDrive = loadDrive;

  // ── Browse / navigate ──
  function setStatus(msg) {
    cdStatusEl.textContent = msg || "";
    cdStatusEl.style.display = msg ? "" : "none";
  }

  async function loadDrive() {
    if (!getToken()) {
      cdListEl.innerHTML = '<div class="cd-empty">Sign in to browse your cloud drive.</div>';
      setStatus("");
      return;
    }
    setStatus("Loading…");
    try {
      const [fRes, foRes] = await Promise.all([apiFetch("/files"), apiFetch("/folders")]);
      drive.files = (fRes.files || []).filter((f) => f.workspace_id == null);
      drive.folders = (foRes.folders || []).filter((d) => d.workspace_id == null);
      setStatus("");
      renderDrive();
    } catch (e) {
      setStatus(`Failed to load cloud drive: ${e.message}`);
    }
  }

  // A folder's parent is its path minus the last segment ("" = My Drive root).
  function parentPath(p) {
    if (!p) return "";
    const i = p.lastIndexOf("/");
    return i < 0 ? "" : p.slice(0, i);
  }

  // All folders to show in the tree: explicit folder rows PLUS virtual/intermediate
  // folders implied by file folder_path prefixes (e.g. a file at "a/b/f.md" implies
  // folders "a" and "a/b" even if no explicit folder row exists). Without these,
  // files nested under un-created intermediate folders would be unreachable.
  function folderList() {
    const seen = new Set();
    const list = [];
    const add = (path) => {
      if (!path || seen.has(path)) return;
      seen.add(path);
      const parts = path.split("/");
      list.push({ name: parts[parts.length - 1], path });
    };
    for (const d of drive.folders) add(d.path);
    for (const f of drive.files) {
      const parts = (f.folder_path || "").split("/").filter(Boolean);
      for (let i = 1; i <= parts.length; i++) add(parts.slice(0, i).join("/"));
    }
    return list;
  }

  // Direct children of a folder path: subfolders + files whose folder_path === path.
  function childrenOf(path) {
    const folderKids = folderList()
      .filter((d) => parentPath(d.path) === path)
      .sort((a, b) => a.name.localeCompare(b.name));
    const fileKids = drive.files
      .filter((f) => (f.folder_path || "") === path)
      .sort((a, b) => a.name.localeCompare(b.name));
    return { folderKids, fileKids };
  }

  function renderDrive() {
    cdListEl.innerHTML = "";
    const { folderKids, fileKids } = childrenOf("");
    if (!folderKids.length && !fileKids.length) {
      const empty = document.createElement("div");
      empty.className = "cd-empty";
      empty.textContent = "My Drive is empty. Right-click or use 📝 / 📁 to add files.";
      cdListEl.appendChild(empty);
      return;
    }
    renderEntries("", 0, cdListEl);
  }

  // Recursively renders folder rows (▸/▾ expandable) then file rows, indented by depth.
  function renderEntries(path, depth, container) {
    const { folderKids, fileKids } = childrenOf(path);
    for (const d of folderKids) {
      const { folderKids: kids, fileKids: kf } = childrenOf(d.path);
      const hasKids = kids.length > 0 || kf.length > 0;
      const open = drive.expanded.has(d.path);
      const row = document.createElement("div");
      row.className = "cd-row cd-folder";
      row.dataset.path = d.path;
      row.dataset.id = d.id || "";
      row.style.paddingLeft = `${6 + depth * 16}px`;
      row.innerHTML = `<span class="cd-tw">${hasKids ? (open ? "▾" : "▸") : "·"}</span>` +
        '<span class="cd-icon">📁</span><span class="cd-name"></span>';
      row.querySelector(".cd-name").textContent = d.name;
      row.title = `${open ? "Collapse" : "Expand"} folder ${d.name}`;
      row.draggable = true;
      row.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", JSON.stringify({ kind: "folder", path: d.path }));
        e.dataTransfer.effectAllowed = "move";
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", () => row.classList.remove("dragging"));
      row.addEventListener("click", () => {
        if (drive.expanded.has(d.path)) drive.expanded.delete(d.path);
        else drive.expanded.add(d.path);
        renderDrive();
      });
      container.appendChild(row);
      if (open) {
        const kidsBox = document.createElement("div");
        kidsBox.className = "cd-kids";
        renderEntries(d.path, depth + 1, kidsBox);
        container.appendChild(kidsBox);
      }
    }
    for (const f of fileKids) {
      const isText = isTextFile(f);
      const row = document.createElement("div");
      row.className = "cd-row cd-file";
      row.style.paddingLeft = `${6 + depth * 16}px`;
      row.innerHTML = '<span class="cd-tw"></span>' +
        `<span class="cd-icon">${isText ? "📄" : "📦"}</span>` +
        '<span class="cd-name"></span>' +
        '<span class="cd-meta"></span>';
      row.querySelector(".cd-name").textContent = f.name;
      row.querySelector(".cd-meta").textContent = fmtSize(f.size);
      row.title = isText ? "Open note" : "Open in viewer";
      row.dataset.folder = f.folder_path || "";
      row.dataset.id = f.id;
      row.draggable = true;
      row.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", JSON.stringify({
          kind: "file", id: f.id, name: f.name, folder_path: f.folder_path || "",
        }));
        e.dataTransfer.effectAllowed = "move";
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", () => row.classList.remove("dragging"));
      row.addEventListener("click", () => { if (isText) openNote(f); else openCloudFile(f); });
      container.appendChild(row);
    }
  }

  // ── Right-click context menu (New text file / New folder / Delete) ──
  // ctx = { folderPath, file?, folder? } — file/folder are the clicked entity (if any).
  let ctxMenuEl = null;

  function showCtxMenu(x, y, ctx) {
    closeCtxMenu();
    const { folderPath, file, folder } = ctx;
    ctxMenuEl = document.createElement("div");
    ctxMenuEl.className = "drive-ctxmenu";
    const mk = (label, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.addEventListener("click", () => { closeCtxMenu(); fn(); });
      return b;
    };
    ctxMenuEl.appendChild(mk("📄 New text file", () => createTextFile(folderPath)));
    ctxMenuEl.appendChild(mk("📁 New folder", () => createFolder(folderPath)));
    ctxMenuEl.appendChild(mk("📤 Upload file", () => uploadFile(folderPath)));
    if (file) {
      const sep = document.createElement("div");
      sep.className = "drive-ctxmenu-sep";
      ctxMenuEl.appendChild(sep);
      ctxMenuEl.appendChild(mk("🗑 Delete file", () => deleteFile(file)));
    } else if (folder) {
      const sep = document.createElement("div");
      sep.className = "drive-ctxmenu-sep";
      ctxMenuEl.appendChild(sep);
      ctxMenuEl.appendChild(mk("🗑 Delete folder", () => deleteFolder(folder)));
    }
    ctxMenuEl.style.left = `${Math.min(x, window.innerWidth - 200)}px`;
    ctxMenuEl.style.top = `${Math.min(y, window.innerHeight - 120)}px`;
    document.body.appendChild(ctxMenuEl);
  }

  function closeCtxMenu() {
    if (ctxMenuEl) { ctxMenuEl.remove(); ctxMenuEl = null; }
  }

  // Delete a cloud file → moves to Trash (recoverable from the web console).
  async function deleteFile(f) {
    if (!getToken()) { Viewer.toast("Sign in to delete cloud files."); return; }
    const ok = await window.confirmModal({
      title: "Delete file?",
      message: `Delete "${f.name}" from your cloud drive?\nIt moves to Trash (recoverable in the web console).`,
      okLabel: "Delete",
    });
    if (!ok) return;
    try {
      await apiFetch(`/files/${f.id}`, { method: "DELETE" });
      setStatus(`Deleted "${f.name}".`);
      loadDrive();
    } catch (e) {
      setStatus(`Delete failed: ${e.message}`);
    }
  }

  // Delete a cloud folder → trashes it and everything inside it. Explicit folders
  // go through DELETE /folders/{id}; purely virtual folders (implied only by file
  // folder_path prefixes, so they have no id) delete each contained file instead.
  async function deleteFolder(d) {
    if (!getToken()) { Viewer.toast("Sign in to delete cloud folders."); return; }
    const ok = await window.confirmModal({
      title: "Delete folder?",
      message: `Delete "${d.name}" and everything inside it?\nIt moves to Trash (recoverable in the web console).`,
      okLabel: "Delete",
    });
    if (!ok) return;
    try {
      if (d.id) {
        await apiFetch(`/folders/${d.id}`, { method: "DELETE" });
      } else {
        const prefix = d.path ? `${d.path}/` : "";
        const under = drive.files.filter((f) => {
          const fp = f.folder_path || "";
          return fp === d.path || (prefix && fp.startsWith(prefix));
        });
        if (!under.length) { setStatus(`Nothing to delete in "${d.name}".`); return; }
        await Promise.all(under.map((f) => apiFetch(`/files/${f.id}`, { method: "DELETE" })));
        setStatus(`Deleted "${d.name}" (${under.length} file${under.length > 1 ? "s" : ""}).`);
        loadDrive();
        return;
      }
      setStatus(`Deleted "${d.name}".`);
      loadDrive();
    } catch (e) {
      setStatus(`Delete failed: ${e.message}`);
    }
  }

  // ── Drag-and-drop move ──
  // Rows are draggable (folder → its path, file → its id). Valid drop targets are
  // folder rows (move into that folder) and empty list area / the "☁️ My Drive"
  // label (move to root). File rows are not drop targets.
  function dropTargetFor(e) {
    const folderRow = e.target.closest(".cd-folder");
    if (folderRow) return { el: folderRow, parent: folderRow.dataset.path || "" };
    if (e.target.closest(".cd-file")) return null; // files aren't containers
    return { el: cdListEl, parent: "" }; // empty area → My Drive root
  }
  function clearDropTargets() {
    cdListEl.querySelectorAll(".drop-target").forEach((el) => el.classList.remove("drop-target"));
  }
  function dragPayload(e) {
    try { return JSON.parse(e.dataTransfer.getData("text/plain") || "null"); } catch { return null; }
  }
  // The server auto-suffixes busy names ("docs" → "docs (1)"). Surface that to the user.
  function renameHint(requested, final) {
    if (requested && final && requested !== final) {
      Viewer.toast(`"${requested}" already exists — used "${final}" instead.`);
    }
  }

  async function doMove(payload, parent) {
    if (!payload) return;
    try {
      if (payload.kind === "file") {
        if ((payload.folder_path || "") === parent) return; // already there
        const res = await apiFetch(`/files/${payload.id}/move`, {
          method: "POST",
          body: JSON.stringify({ workspace_id: null, folder_path: parent || null }),
        });
        renameHint(payload.name, res.name);
        setStatus(`Moved "${res.name}".`);
      } else if (payload.kind === "folder") {
        const src = payload.path;
        if (src === parent) return; // dropped back onto itself
        if (parent && (parent === src || parent.startsWith(src + "/"))) {
          setStatus("Can't move a folder into itself.");
          return;
        }
        const name = src.split("/").pop();
        const row = drive.folders.find((d) => d.path === src);
        if (row) {
          const res = await apiFetch(`/folders/${row.id}/move`, {
            method: "POST",
            body: JSON.stringify({ parent_path: parent || null }),
          });
          const finalName = res.name || String(res.path || "").split("/").pop();
          renameHint(name, finalName);
          setStatus(`Moved "${finalName}".`);
        } else {
          // Virtual folder (implied only by file prefixes): re-parent every file under it.
          const newPath = parent ? `${parent}/${name}` : name;
          let renamed = null;
          for (const f of drive.files) {
            const fp = f.folder_path || "";
            if (fp === src || fp.startsWith(src + "/")) {
              const suffix = fp === src ? "" : fp.slice(src.length);
              const res = await apiFetch(`/files/${f.id}/move`, {
                method: "POST",
                body: JSON.stringify({ workspace_id: null, folder_path: (newPath + suffix) || null }),
              });
              if (res.name !== f.name) renamed = res.name;
            }
          }
          if (renamed) renameHint(name, renamed);
          setStatus(`Moved "${name}".`);
        }
      }
      loadDrive(); // server is the source of truth; refresh the whole tree
    } catch (e) {
      setStatus(`Move failed: ${e.message}`);
    }
  }
  cdListEl.addEventListener("dragover", (e) => {
    const t = dropTargetFor(e);
    if (!t) return; // over a file row → no drop allowed
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    clearDropTargets();
    t.el.classList.add("drop-target");
  });
  cdListEl.addEventListener("dragleave", (e) => {
    if (!cdListEl.contains(e.relatedTarget)) clearDropTargets();
  });
  cdListEl.addEventListener("drop", (e) => {
    e.preventDefault();
    clearDropTargets();
    const t = dropTargetFor(e);
    if (!t) return;
    doMove(dragPayload(e), t.parent);
  });
  cdListEl.addEventListener("dragend", clearDropTargets);
  cdPathEl.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    cdPathEl.classList.add("drop-target");
  });
  cdPathEl.addEventListener("dragleave", () => cdPathEl.classList.remove("drop-target"));
  cdPathEl.addEventListener("drop", (e) => {
    e.preventDefault();
    cdPathEl.classList.remove("drop-target");
    doMove(dragPayload(e), "");
  });

  function isTextFile(f) {
    const mime = String(f.mime_type || "").toLowerCase();
    if (mime.startsWith("text/")) return true;
    return /\.(txt|md|markdown|text|log|json|csv|yaml|yml|toml|ini|xml|html|py|js|ts|jsx|tsx|c|h|cpp|hpp|java|go|rs|sh|bat|sql)$/i.test(f.name || "");
  }

  function fmtSize(n) {
    if (n == null) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  // ── New folder / new text file ──
  async function createFolder(parent = "") {
    if (!getToken()) { Viewer.toast("Sign in to create cloud folders."); return; }
    const name = await window.promptModal({ title: "New folder", placeholder: "Folder name", initial: "" });
    if (!name) return;
    const requested = name.trim();
    try {
      const res = await apiFetch("/folders", {
        method: "POST",
        body: JSON.stringify({ name: requested, parent_path: parent || null }),
      });
      drive.folders.push(res);
      if (parent) drive.expanded.add(parent);
      renameHint(requested, res.name);
      setStatus(`Folder "${res.name}" created.`);
      renderDrive();
    } catch (e) {
      setStatus(`Failed to create folder: ${e.message}`);
    }
  }

  async function createTextFile(parent = "") {
    if (!getToken()) { Viewer.toast("Sign in to create cloud notes."); return; }
    const name = await window.promptModal({ title: "New text file", placeholder: "File name", initial: "untitled.txt" });
    if (!name) return;
    const content = await window.promptModal({
      title: "Initial content (optional)", placeholder: "Markdown / plain text", initial: "",
      multiline: true, okLabel: "OK",
    });
    if (content === null) return; // cancelled
    const finalName = /\.\w+$/.test(name) ? name : `${name}.txt`;
    try {
      const bytes = new TextEncoder().encode(content);
      const hex = toHex(await crypto.subtle.digest("SHA-256", bytes));
      const init = await apiFetch("/files/init-upload", {
        method: "POST",
        body: JSON.stringify({
          sha256: hex,
          size: bytes.length,
          name: finalName,
          folder_path: parent || null,
          mime_type: "text/plain",
        }),
      });
      if (init.status !== "instant" && init.asset_id) {
        const chunkSize = init.chunk_size || 5 * 1024 * 1024;
        const num = init.num_chunks || Math.ceil(bytes.length / chunkSize);
        for (let i = 0; i < num; i++) {
          const start = i * chunkSize;
          const slice = bytes.slice(start, Math.min(bytes.length, start + chunkSize));
          const headers = { Authorization: `Bearer ${getToken()}` };
          const put = await fetch(`/api/files/${init.asset_id}/chunks/${i}`, {
            method: "PUT",
            headers,
            body: new Blob([slice]),
          });
          if (!put.ok) throw new Error(`chunk ${i} failed (${put.status})`);
        }
        await apiFetch(`/files/${init.asset_id}/complete`, { method: "POST" });
      }
      // Server may have auto-suffixed the name to keep the folder unique — fetch the result.
      const created = init.status === "instant" && init.asset
        ? init.asset
        : await apiFetch(`/files/${init.asset_id}`);
      if (parent) drive.expanded.add(parent);
      renameHint(finalName, created.name);
      setStatus(`Created "${created.name}".`);
      loadDrive();
    } catch (e) {
      setStatus(`Failed to create text file: ${e.message}`);
    }
  }

  function toHex(buf) {
    return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  // ── Upload a local file to the cloud drive ──
  // Mirrors the web console: a hidden <input type="file"> (native OS dialog), then the
  // same init-upload → chunked PUT → complete flow as createTextFile. The server is the
  // single source of truth, so a dedup-renamed name is surfaced via renameHint.
  function pickLocalFile() {
    return new Promise((resolve) => {
      const input = document.createElement("input");
      input.type = "file";
      input.style.display = "none";
      input.addEventListener("change", () => {
        const f = input.files && input.files[0] ? input.files[0] : null;
        input.remove();
        resolve(f);
      });
      document.body.appendChild(input);
      input.click();
    });
  }

  async function uploadFile(parent = "") {
    if (!getToken()) { Viewer.toast("Sign in to upload files."); return; }
    const file = await pickLocalFile();
    if (!file) return;
    setStatus(`Uploading "${file.name}"…`);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const hex = toHex(await crypto.subtle.digest("SHA-256", bytes));
      const init = await apiFetch("/files/init-upload", {
        method: "POST",
        body: JSON.stringify({
          sha256: hex,
          size: bytes.length,
          name: file.name,
          folder_path: parent || null,
          mime_type: file.type || null,
        }),
      });
      let created;
      if (init.status !== "instant") {
        const chunkSize = init.chunk_size || 5 * 1024 * 1024;
        const num = init.num_chunks || Math.ceil(bytes.length / chunkSize);
        for (let i = 0; i < num; i++) {
          const start = i * chunkSize;
          const slice = bytes.slice(start, Math.min(bytes.length, start + chunkSize));
          const headers = { Authorization: `Bearer ${getToken()}` };
          const put = await fetch(`/api/files/${init.asset_id}/chunks/${i}`, {
            method: "PUT",
            headers,
            body: new Blob([slice]),
          });
          if (!put.ok) throw new Error(`chunk ${i} failed (${put.status})`);
          setStatus(`Uploading "${file.name}" ${i + 1}/${num}…`);
        }
        await apiFetch(`/files/${init.asset_id}/complete`, { method: "POST" });
        created = await apiFetch(`/files/${init.asset_id}`);
      } else {
        created = init.asset || await apiFetch(`/files/${init.asset_id}`);
      }
      if (parent) drive.expanded.add(parent);
      renameHint(file.name, created.name);
      setStatus(init.status === "instant"
        ? `Uploaded instantly (deduplicated) "${created.name}".`
        : `Uploaded "${created.name}".`);
      loadDrive();
    } catch (e) {
      setStatus(`Upload failed: ${e.message}`);
    }
  }

  // ── Open a binary cloud file in the in-window viewer ──
  // The viewer is path-based (it streams via the `local://` protocol and reads PDF
  // annotation sidecars), so the main process first caches the file from the server
  // into a per-asset temp path, then we render that path with the file's real name.
  async function openCloudFile(f) {
    if (!getToken()) { Viewer.toast("Sign in to open cloud files."); return; }
    // The note editor overlays the viewer area (opaque, z-index 30), so a file opened
    // in the viewer while a note is up would render behind it. Close the editor first
    // (confirms on unsaved changes); if the user cancels, abort the open.
    await closeNote();
    if (note.asset) return;
    setStatus(`Opening "${f.name}"…`);
    try {
      const res = await window.desktopAPI.cloudCache(f.id, f.name, getToken());
      if (!res.ok) { setStatus(`Failed to open "${f.name}": ${res.error}`); return; }
      setStatus("");
      Viewer.render(res.path, f.name);
    } catch (e) {
      setStatus(`Failed to open "${f.name}": ${e.message}`);
    }
  }

  // ── Note editor ──
  async function openNote(f) {
    if (!getToken()) { Viewer.toast("Sign in to open cloud notes."); return; }
    if (note.dirty && !(await window.confirmModal("Discard unsaved changes to the current note?"))) return;
    setStatus("Loading note…");
    try {
      const res = await apiFetch(`/files/${f.id}/content`);
      note.asset = f;
      note.dirty = false;
      note.preview = false;
      noteTextarea.value = res.content || "";
      noteTitleEl.textContent = f.folder_path ? `${f.folder_path}/${f.name}` : f.name;
      setPreviewMode(false);
      noteSaveBtn.disabled = true;
      noteSaveBtn.textContent = "💾 Save";
      noteEditor.classList.remove("hidden");
      setStatus("");
      noteTextarea.focus();
    } catch (e) {
      setStatus(`Failed to open note: ${e.message}`);
    }
  }

  async function saveNote() {
    if (!note.asset) return;
    noteSaveBtn.disabled = true;
    noteSaveBtn.textContent = "💾 Saving…";
    try {
      const res = await apiFetch(`/files/${note.asset.id}/content`, {
        method: "PUT",
        body: JSON.stringify({ content: noteTextarea.value }),
      });
      note.asset = { ...note.asset, size: res.asset?.size ?? note.asset.size, updated_at: res.asset?.updated_at ?? note.asset.updated_at };
      note.dirty = false;
      noteSaveBtn.textContent = "💾 Saved ✓";
      setTimeout(() => { noteSaveBtn.textContent = "💾 Save"; }, 1500);
      loadDrive(); // refresh size/updated_at in the sidebar list
      setStatus("Note saved. Re-indexing in background…");
    } catch (e) {
      noteSaveBtn.disabled = false;
      noteSaveBtn.textContent = "💾 Save";
      setStatus(`Save failed: ${e.message}`);
    }
  }

  function setPreviewMode(on) {
    note.preview = on;
    notePreviewPane.classList.toggle("hidden", !on);
    noteTextarea.classList.toggle("hidden", on);
    noteModeEl.textContent = on ? "👁 preview" : "✏️ edit";
    notePreviewBtn.textContent = on ? "✏️ Edit" : "👁 Preview";
    if (on) {
      notePreviewPane.innerHTML = renderMarkdown(noteTextarea.value);
    } else {
      noteTextarea.focus();
    }
  }

  async function closeNote() {
    if (note.dirty && !(await window.confirmModal("Discard unsaved changes to the current note?"))) return;
    note.asset = null;
    note.dirty = false;
    noteEditor.classList.add("hidden");
    noteTextarea.value = "";
    notePreviewPane.innerHTML = "";
    noteSaveBtn.disabled = false;
    noteSaveBtn.textContent = "💾 Save";
  }

  // Markdown rendering, XSS-safe: html:false escapes raw HTML and the link validator
  // only allows http/https/mailto + relative/anchor hrefs (mirrors app.js isSafeLink).
  function isSafeLink(url) {
    const u = String(url || "").trim().toLowerCase();
    if (!u) return false;
    if (u.startsWith("http://") || u.startsWith("https://") || u.startsWith("mailto:")) return true;
    if (u.startsWith("/") || u.startsWith("./") || u.startsWith("../") || u.startsWith("#")) return true;
    return !/^[a-z][a-z0-9+.-]*:/i.test(u);
  }
  function renderMarkdown(text) {
    const src = String(text ?? "");
    const md = window.markdownit && window.markdownit({
      html: false,
      linkify: true,
      breaks: true,
      validateLink: isSafeLink,
    });
    if (!md) return escapeHtml(src);
    return md.render(src);
  }
  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // ── Search (client-side fuzzy autocomplete) ──
  // All My Drive files/folders are already loaded, so suggestions are computed locally
  // while typing — same scoring as the web console. Clicking a suggestion reveals that
  // entry in the tree (expands its ancestors) and flashes it.
  let suggestIndex = -1;

  // Case-insensitive fuzzy score; lower is better, null = no match. Ranked: exact >
  // name prefix > name substring > folder-path hit > loose subsequence of the name.
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

  function searchCandidates(q) {
    const scored = [];
    for (const f of drive.files) {
      const n = fuzzyScore(f.name, q);
      const p = f.folder_path ? fuzzyScore(f.folder_path, q) : null;
      const score = n != null ? n : p != null ? 1000 + p : null;
      if (score != null) scored.push({ score, item: { kind: "file", f, name: f.name, path: f.folder_path || "" } });
    }
    for (const d of folderList()) {
      const n = fuzzyScore(d.name, q);
      const p = fuzzyScore(d.path, q);
      const score = n != null ? n : p != null ? 1000 + p : null;
      if (score != null) scored.push({ score, item: { kind: "folder", d, name: d.name, path: d.path } });
    }
    scored.sort((a, b) => a.score - b.score);
    return scored.slice(0, 10).map((s) => s.item);
  }

  function hideSuggest() {
    cdSuggest.classList.add("hidden");
    cdSuggest.innerHTML = "";
    suggestIndex = -1;
  }

  function renderSuggestions(q) {
    cdSuggest.innerHTML = "";
    if (!q) { hideSuggest(); return; }
    const items = searchCandidates(q);
    if (!items.length) { hideSuggest(); return; }
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "cd-suggest-item";
      const icon = document.createElement("span");
      icon.className = "cd-suggest-icon";
      icon.textContent = it.kind === "file" ? (isTextFile(it.f) ? "📄" : "📦") : "📁";
      const name = document.createElement("span");
      name.className = "cd-suggest-name";
      name.textContent = it.name;
      const meta = document.createElement("span");
      meta.className = "cd-suggest-meta";
      meta.textContent = it.path || "";
      row.append(icon, name, meta);
      row.addEventListener("mousedown", (e) => {
        e.preventDefault();
        if (it.kind === "file") jumpToFile(it.f); else jumpToFolder(it.d);
      });
      cdSuggest.appendChild(row);
    }
    cdSuggest.classList.remove("hidden");
  }

  // Expand every ancestor folder of `path` so a search hit inside it becomes visible.
  function expandAncestors(path) {
    let cur = "";
    for (const part of String(path || "").split("/")) {
      if (!part) continue;
      cur = cur ? `${cur}/${part}` : part;
      drive.expanded.add(cur);
    }
  }

  function revealRow(row) {
    if (!row) { setStatus("Not found — refresh the drive."); return; }
    row.scrollIntoView({ block: "nearest" });
    row.classList.add("cd-flash");
    setTimeout(() => row.classList.remove("cd-flash"), 1400);
  }

  function clearSearch() {
    cdSearch.value = "";
    cdSearchClear.classList.remove("visible");
    hideSuggest();
  }

  function jumpToFile(f) {
    expandAncestors(f.folder_path);
    renderDrive();
    revealRow(cdListEl.querySelector(`[data-id="${f.id}"]`));
    clearSearch();
  }

  function jumpToFolder(d) {
    expandAncestors(d.path);
    renderDrive();
    revealRow(cdListEl.querySelector(`[data-path="${d.path.replace(/"/g, '\\"')}"]`));
    clearSearch();
  }

  // Wire the search box (input + clear + keyboard nav + outside-click close).
  cdSearch.addEventListener("input", (e) => {
    cdSearchClear.classList.toggle("visible", !!e.target.value);
    renderSuggestions(e.target.value.trim());
  });
  cdSearchClear.addEventListener("click", () => {
    clearSearch();
    cdSearch.focus();
  });
  cdSearch.addEventListener("keydown", (e) => {
    const items = cdSuggest.querySelectorAll(".cd-suggest-item");
    if (e.key === "Escape") {
      e.preventDefault();
      clearSearch();
      cdSearch.blur();
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!items.length) return;
      const dir = e.key === "ArrowDown" ? 1 : -1;
      suggestIndex = (suggestIndex + dir + items.length) % items.length;
      items.forEach((el, i) => el.classList.toggle("active", i === suggestIndex));
      items[suggestIndex].scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (!items.length) return;
      items[suggestIndex >= 0 ? suggestIndex : 0].dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    }
  });
  document.addEventListener("mousedown", (e) => {
    if (!cdSuggest.classList.contains("hidden") && !cdSearch.closest(".search-wrap").contains(e.target)) {
      hideSuggest();
    }
  });

  // ── Wiring ──
  cdNewFolder.addEventListener("click", () => createFolder(""));
  cdNewText.addEventListener("click", () => createTextFile(""));
  cdListEl.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    // Right-click a folder → create inside it / delete it; a file → create in its
    // folder / delete it; empty area → create at My Drive root.
    const folderRow = e.target.closest(".cd-folder");
    const fileRow = e.target.closest(".cd-file");
    const ctx = { folderPath: "", file: null, folder: null };
    if (folderRow) {
      const p = folderRow.dataset.path;
      ctx.folderPath = p;
      ctx.folder = drive.folders.find((d) => d.path === p) || { name: p.split("/").pop(), path: p };
    } else if (fileRow) {
      ctx.folderPath = fileRow.dataset.folder || "";
      ctx.file = drive.files.find((f) => f.id === fileRow.dataset.id);
    }
    showCtxMenu(e.clientX, e.clientY, ctx);
  });
  document.addEventListener("mousedown", (e) => {
    if (ctxMenuEl && !ctxMenuEl.contains(e.target)) closeCtxMenu();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCtxMenu(); });
  notePreviewBtn.addEventListener("click", () => setPreviewMode(!note.preview));
  noteSaveBtn.addEventListener("click", saveNote);
  noteCloseBtn.addEventListener("click", closeNote);
  noteTextarea.addEventListener("input", () => {
    note.dirty = true;
    noteSaveBtn.disabled = false;
  });
  noteTextarea.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveNote();
    }
  });
  document.addEventListener("keydown", (e) => {
    // Ctrl+O opens a local folder in the sidebar (default Electron behavior would be
    // a browser prompt); leave that alone. Esc closes the note editor when open.
    if (e.key === "Escape" && !noteEditor.classList.contains("hidden")) {
      e.preventDefault();
      closeNote();
    }
  });

  // If a cloud drive is already visible (e.g. re-opened window), refresh it.
  if (!cloudEl.classList.contains("hidden")) loadDrive();
})();
