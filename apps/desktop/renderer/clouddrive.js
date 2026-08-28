// Cloud drive browser + markdown note editor for the desktop sidebar.
//
// The server is the single source of truth: this panel calls the same REST API the
// web console uses, so edits here show up in the web console on refresh and vice
// versa. The main process proxies /api/* to the backend, and auth reuses the session
// token app.js stores in localStorage["deepdive_token"] — this module is a separate
// IIFE so it reads that token directly instead of reaching into app.js's private scope.
//
// The main area mirrors the web CloudDrive: a five-column table (Name / Size / RAG
// Status / Query Repo / Updated) with list↔grid view toggle, per-folder search,
// edit mode + batch actions, and the same workspace / trash / sharing semantics.
// The sidebar tree shows My Drive, one node per workspace, and Trash at the bottom.
(() => {
  const TOKEN_KEY = "deepdive_token";

  const cloudEl = document.getElementById("clouddrive");
  const cdPathEl = document.getElementById("cd-path");
  const cdListEl = document.getElementById("cd-list");
  const cdStatusEl = document.getElementById("cd-status");
  const cdNewFolder = document.getElementById("cd-new-folder");
  const cdNewText = document.getElementById("cd-new-text");
  const cdNewWs = document.getElementById("cd-new-workspace");
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

  // ── State ──
  // Everything the main area needs, mirroring the web CloudDrive component state.
  // drive.expanded holds scoped tree-expansion keys (see expKey) so the same folder
  // name in two workspaces stays independently expandable.
  const drive = {
    expanded: new Set(),
    files: [],          // every file across My Drive + all workspaces
    folders: [],        // every folder row across all scopes
    workspaces: [],
    trash: [],
    me: null,
    loc: { kind: "root" },           // current main-area location
    viewMode: "list",                // "list" | "grid"
    query: "",                       // main-area search filter (current folder)
    editMode: false,
    selected: new Set(),             // selected file ids in the current folder
    importState: {},                 // id → "importing" | "ok" | "err"
    importError: {},
    ingestStart: {},                 // id → ms when it entered a WORKING phase
  };
  const note = { asset: null, dirty: false, preview: false };
  // A background refresh (5s ingest poll, import completion, etc.) must never clobber a
  // document the user is reading. When one is open we only re-render the sidebar tree.
  let refreshPending = false;

  // The cloud asset (if any) currently open in the in-window viewer — used by the chat
  // "attach current file" action. app.js reads it via window.__getViewerCloudFile().
  let viewerCloudFile = null;
  window.__getViewerCloudFile = () => viewerCloudFile;
  window.__setViewerCloudFile = (f) => { viewerCloudFile = f || null; };

  // ── Location model (mirrors apps/web/src/CloudDrive.tsx Loc) ──
  // A workspace is a top-level scope; a "folder" loc is a subfolder inside a
  // workspace (or My Drive when ws is null).
  // loc = { kind: "root" } | { kind: "workspace", ws } | { kind: "folder", ws, path } | { kind: "trash" }
  function locKey(l) {
    if (l.kind === "root") return "root";
    if (l.kind === "trash") return "trash";
    if (l.kind === "workspace") return `ws:${l.ws}`;
    return l.ws ? `ws:${l.ws}/${l.path}` : l.path;
  }
  function wsName(wsId) {
    if (!wsId) return "My Drive";
    const w = drive.workspaces.find((x) => x.id === wsId);
    return w ? w.name : "Workspace";
  }
  function locLabel(l) {
    if (l.kind === "root") return "My Drive";
    if (l.kind === "trash") return "Trash";
    if (l.kind === "workspace") return wsName(l.ws);
    const base = l.ws ? wsName(l.ws) : "My Drive";
    return l.path ? `${base} / ${l.path.split("/").join(" / ")}` : base;
  }
  function expKey(ws, path) {
    return ws == null ? (path || "root") : `ws:${ws}/${path || ""}`;
  }
  // Whether the main-area toolbar should show the ⚙ Manage button (workspace scopes).
  function inWs(loc) {
    return loc.kind === "workspace" || (loc.kind === "folder" && loc.ws != null);
  }
  function locKindWs(loc) {
    return loc.kind === "workspace" || loc.kind === "folder" ? loc.ws : null;
  }
  function locFolderPath(loc) {
    return loc.kind === "folder" ? loc.path : null;
  }

  // ── RAG import status (mirrors apps/web/src/CloudDrive.tsx) ──
  const RAG_NOT_STARTED = "NOT_STARTED";
  const RAG_WORKING = new Set(["PENDING", "PARSING", "CHUNKING", "EMBEDDING"]);
  const RAG_LABEL = {
    NOT_STARTED: "—",
    PENDING: "Pending",
    PARSING: "Parsing",
    CHUNKING: "Chunking",
    EMBEDDING: "Embedding",
    INDEXED: "Indexed",
    FAILED: "Failed",
  };
  const RAG_IMPORTABLE_EXTS = new Set([
    ".txt", ".md", ".markdown", ".text", ".log", ".json", ".csv",
    ".srt", ".vtt", ".lrc", ".pdf", ".docx",
  ]);

  // Lowercase extension including the dot ("" when none), e.g. "note.md" → ".md".
  function fileExt(name) {
    const i = name.lastIndexOf(".");
    return i < 0 ? "" : name.slice(i).toLowerCase();
  }

  // Badge color class mirroring the web console: indexed green / failed red /
  // working amber / pending gray.
  function ragClass(s) {
    if (s === "INDEXED") return "rag-indexed";
    if (s === "FAILED") return "rag-failed";
    return RAG_WORKING.has(s) ? "rag-working" : "rag-pending";
  }

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

  // Open the Cloud Drive main view at a My Drive folder path ("" = root). Used by the
  // toolkit generate dialog's "view output" link so the user can see where a generated
  // artifact landed instead of hunting for it.
  window.openCloudFolder = async (folderPath) => {
    try { await loadDrive(); } catch { /* keep whatever list we already have */ }
    const rel = (folderPath || "").replace(/^\/+|\/+$/g, "");
    navigate(rel ? { kind: "folder", ws: null, path: rel } : { kind: "root" });
  };

  // ── Browse / navigate ──
  function setStatus(msg) {
    cdStatusEl.textContent = msg || "";
    cdStatusEl.style.display = msg ? "" : "none";
  }

  // Record (once) the time each file enters a WORKING rag phase, for the countdown.
  function updateIngestStart() {
    let changed = false;
    for (const f of drive.files) {
      if (RAG_WORKING.has(f.rag_status) && !(f.id in drive.ingestStart)) {
        drive.ingestStart[f.id] = Date.now();
        changed = true;
      }
    }
    return changed;
  }

  // After a reload a folder/workspace may have been deleted — fall back to its scope root.
  function locStillValid(l) {
    if (l.kind === "root" || l.kind === "trash") return true;
    if (l.kind === "workspace") return drive.workspaces.some((x) => x.id === l.ws);
    const ws = l.ws;
    if (drive.folders.some((d) => d.workspace_id === ws && d.path === l.path)) return true;
    return drive.files.some((f) => f.workspace_id === ws &&
      (f.folder_path === l.path || (l.path && (f.folder_path || "").startsWith(l.path + "/"))));
  }
  function scopeRoot(l) {
    if (l.kind === "trash") return { kind: "trash" };
    if (l.kind === "workspace") return { kind: "workspace", ws: l.ws };
    if (l.kind === "folder") return l.ws ? { kind: "workspace", ws: l.ws } : { kind: "root" };
    return { kind: "root" };
  }

  async function loadDrive() {
    if (!getToken()) {
      cdListEl.innerHTML = '<div class="cd-empty">Sign in to browse your cloud drive.</div>';
      setStatus("");
      return;
    }
    setStatus("Loading…");
    try {
      const [fRes, foRes, wsRes, trRes, meRes] = await Promise.all([
        apiFetch("/files"),
        apiFetch("/folders"),
        apiFetch("/workspaces"),
        apiFetch("/trash"),
        apiFetch("/auth/me"),
      ]);
      drive.files = fRes.files || [];
      drive.folders = foRes.folders || [];
      drive.workspaces = wsRes.workspaces || [];
      drive.trash = trRes.files || [];
      drive.me = meRes || null;
      updateIngestStart();
      setStatus("");
      if (!locStillValid(drive.loc)) {
        drive.loc = scopeRoot(drive.loc);
        drive.query = "";
        drive.editMode = false;
        drive.selected = new Set();
      }
      renderDrive();
      refreshMain();
      pollWhileWorking();
    } catch (e) {
      setStatus(`Failed to load cloud drive: ${e.message}`);
    }
  }

  // A folder's parent is its path minus the last segment ("" = scope root).
  function parentPath(p) {
    if (!p) return "";
    const i = p.lastIndexOf("/");
    return i < 0 ? "" : p.slice(0, i);
  }

  // All folders to show for a scope (ws null = My Drive): explicit folder rows PLUS
  // virtual/intermediate folders implied by file folder_path prefixes (e.g. a file at
  // "a/b/f.md" implies folders "a" and "a/b" even if no explicit row exists).
  function folderList(ws) {
    const seen = new Set();
    const list = [];
    const add = (path, id) => {
      if (!path || seen.has(path)) return;
      seen.add(path);
      const parts = path.split("/");
      list.push({ name: parts[parts.length - 1], path, id: id || null, workspace_id: ws });
    };
    for (const d of drive.folders) if (d.workspace_id === ws) add(d.path, d.id);
    for (const f of drive.files) {
      if (f.workspace_id !== ws) continue;
      const parts = (f.folder_path || "").split("/").filter(Boolean);
      for (let i = 1; i <= parts.length; i++) add(parts.slice(0, i).join("/"), null);
    }
    return list;
  }
  function allFolders() {
    const out = [...folderList(null)];
    for (const w of drive.workspaces) out.push(...folderList(w.id));
    return out;
  }

  // Direct children of a folder path within a scope: subfolders + files whose path matches.
  function childrenOf(ws, path) {
    const folderKids = folderList(ws)
      .filter((d) => parentPath(d.path) === path)
      .sort((a, b) => a.name.localeCompare(b.name));
    const fileKids = drive.files
      .filter((f) => f.workspace_id === ws && (f.folder_path || "") === path)
      .sort((a, b) => a.name.localeCompare(b.name));
    return { folderKids, fileKids };
  }

  // Normalize the current location's children into the opaque entries contract the
  // shared Viewer.renderFolder expects (dirs carry {type,name,path}; files pass whole).
  function cloudEntriesFor(loc) {
    if (loc.kind === "trash") return drive.trash.map((f) => ({ type: "file", ...f }));
    const ws = loc.kind === "root" ? null : loc.ws;
    const path = loc.kind === "folder" ? loc.path : "";
    const { folderKids, fileKids } = childrenOf(ws, path);
    return folderKids
      .map((d) => ({ type: "dir", ...d }))
      .concat(fileKids.map((f) => ({ type: "file", ...f })));
  }

  // Entries shown in the main area after the search filter is applied.
  function currentEntries() {
    let entries = cloudEntriesFor(drive.loc);
    const q = drive.query.trim().toLowerCase();
    if (q) entries = entries.filter((e) => (e.name || "").toLowerCase().includes(q));
    return entries;
  }
  function currentLocFiles() {
    return currentEntries().filter((e) => e.type !== "dir");
  }

  function canWriteAt(loc) {
    if (loc.kind === "trash") return false;
    if (loc.kind === "root" || loc.ws == null) return true; // My Drive is personal
    const w = drive.workspaces.find((x) => x.id === loc.ws);
    if (!w) return false;
    return w.role === "owner" || w.role === "admin" || w.role === "editor";
  }
  function canManageAt(loc) {
    if (loc.kind === "trash") return false;
    if (loc.kind === "root" || loc.ws == null) return false;
    const w = drive.workspaces.find((x) => x.id === loc.ws);
    if (!w) return false;
    return w.role === "owner" || w.role === "admin";
  }

  // Central navigation: update the location, reset per-folder UI state, re-render.
  // User-initiated, so it always switches the main area (even over an open document) —
  // matching the local-tree behavior where clicking a folder replaces the viewer.
  function navigate(loc) {
    drive.loc = loc;
    drive.query = "";
    drive.editMode = false;
    drive.selected = new Set();
    renderDrive();
    refreshPending = false;
    browseCloudFolder(drive.loc);
  }

  // Re-render the main area from the current state. Background refreshes (loadDrive /
  // the 5s ingest poll) skip this while a document is open so the file the user is
  // reading is never torn down underneath them; it comes back on the next refresh once
  // the document is closed. `refreshPending` marks that a re-render is owed.
  function refreshMain() {
    if (!drive.loc) return;
    if (Viewer.isOpen()) { refreshPending = true; return; }
    refreshPending = false;
    browseCloudFolder(drive.loc);
  }

  // Re-render the main area but keep the scroll position (checkbox toggles, search,
  // view switch all rebuild the DOM from scratch).
  function rerenderMainPreserve() {
    const body = document.querySelector("#viewer .cdt-body");
    const st = body ? body.scrollTop : null;
    refreshMain();
    if (st != null) {
      const b2 = document.querySelector("#viewer .cdt-body");
      if (b2) b2.scrollTop = st;
    }
  }

  function toggleEditMode() {
    drive.editMode = !drive.editMode;
    if (!drive.editMode) drive.selected = new Set();
    refreshMain();
  }

  function setMainQuery(q) {
    drive.query = q;
    refreshMain();
    // Rebuilding #viewer wipes the input; restore focus so typing keeps working.
    const inp = document.querySelector(".cdt-search");
    if (inp) {
      inp.focus();
      try { inp.setSelectionRange(q.length, q.length); } catch { /* noop */ }
    }
  }

  function browseCloudFolder(loc) {
    const isTrash = loc.kind === "trash";
    const rootName = isTrash ? "Trash" : loc.kind === "root" ? "My Drive" : wsName(loc.ws);
    const pathForCrumbs = loc.kind === "folder" ? loc.path : "";
    Viewer.renderFolder(currentEntries(), {
      cloudTable: true,
      rootPath: "",
      rootName,
      path: pathForCrumbs,
      read: () => ({ entries: currentEntries(), localPath: null }),
      open: (f) => (isTextFile(f) ? openNote(f) : openCloudFile(f)),
      localPath: null,
      onCrumb: (relPath) => {
        if (drive.loc.kind === "trash") return;
        if (!relPath) navigate(drive.loc.ws ? { kind: "workspace", ws: drive.loc.ws } : { kind: "root" });
        else navigate({ kind: "folder", ws: drive.loc.ws, path: relPath });
      },
      cloud: {
        viewMode: drive.viewMode,
        editMode: drive.editMode,
        isTrash,
        canWrite: canWriteAt(loc),
        canManage: canManageAt(loc),
        inWs: inWs(loc),
        trashCount: drive.trash.length,
        selected: drive.selected,
        query: drive.query,
        locLabel: locLabel(loc),
        emptyText: isTrash ? "Trash is empty." : "This folder is empty.",
      },
      onAction: (name) => onMainAction(name),
      onSearch: (q) => setMainQuery(q),
      onEnterFolder: (d) => navigate({ kind: "folder", ws: drive.loc.ws, path: d.path }),
      onOpenEntry: (f) => {
        if (drive.editMode) toggleOne(f.id);
        else if (!isTrash) { if (isTextFile(f)) openNote(f); else openCloudFile(f); }
      },
      onToggleOne: (id) => toggleOne(id),
      onToggleAll: (checked) => toggleAll(checked),
      onDeleteFolder: (d) => deleteFolder(d),
      onBatch: (name) => onBatch(name),
      ragCell: (f) => ragCell(f),
      ragBadge: (f) => ragBadge(f),
    });
  }

  // RAG status badge cell (mirrors the web table / grid).
  function ragBadge(f) {
    const b = document.createElement("span");
    b.className = "badge rag " + ragClass(f.rag_status);
    b.textContent = RAG_LABEL[f.rag_status] ?? f.rag_status;
    return b;
  }

  // Coarse ingest ETA for the "Processing…" countdown (mirrors the web console).
  function estimateIngestSeconds(size, name) {
    const mb = Math.max(0.1, (size || 0) / (1024 * 1024));
    const perMb = String(name || "").toLowerCase().endsWith(".pdf") ? 12 : 6;
    return Math.min(600, Math.round(20 + mb * perMb));
  }
  function ingestEtaRemaining(size, name, startMs) {
    if (!startMs) return 0;
    return Math.max(0, estimateIngestSeconds(size, name) - Math.floor((Date.now() - startMs) / 1000));
  }
  function ingestEtaSuffix(size, name, startMs) {
    const remain = ingestEtaRemaining(size, name, startMs);
    return remain > 0 ? ` · ~${remain}s` : "";
  }

  // Query Repo column button — the same state machine as the web console:
  // ✓ In Knowledge / Importing… / Queued… / Processing…(~Ns) / ＋ Import to Knowledge / Not supported.
  function ragCell(f) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cdt-btn cdt-cell-btn";
    btn.disabled = true;
    const st = drive.importState[f.id];
    if (f.rag_status === "INDEXED") {
      btn.textContent = "✓ In Knowledge";
      btn.title = "Already in knowledge";
    } else if (st === "importing") {
      btn.textContent = "Importing…";
      btn.title = "Importing into the searchable corpus…";
    } else if (RAG_WORKING.has(f.rag_status)) {
      btn.textContent = f.rag_status === "PENDING"
        ? "Queued…"
        : `Processing…${ingestEtaSuffix(f.size, f.name, drive.ingestStart[f.id])}`;
      btn.title = "Already queued / processing — flips to In Knowledge when done";
    } else if (RAG_IMPORTABLE_EXTS.has(fileExt(f.name || ""))) {
      btn.disabled = false;
      if (st === "err") {
        btn.classList.add("cdt-danger");
        btn.textContent = "Failed — retry";
      } else {
        btn.textContent = "＋ Import to Knowledge";
      }
      btn.title = st === "err"
        ? (drive.importError[f.id] || "Import failed")
        : "Import this file into your searchable knowledge";
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        importToRepo(f);
      });
    } else {
      btn.textContent = "Not supported";
      btn.title = "Format not supported";
    }
    return btn;
  }

  async function importToRepo(f) {
    drive.importState[f.id] = "importing";
    refreshMain();
    try {
      await apiFetch(`/files/${f.id}/import-rag`, { method: "POST" });
      drive.importState[f.id] = "ok";
      await loadDrive();
      pollWhileWorking();
    } catch (err) {
      drive.importState[f.id] = "err";
      drive.importError[f.id] = err.message || String(err);
      refreshMain();
      Viewer.toast(`Import failed: ${err.message || err}`);
    }
  }

  // While any file (across scopes) is mid-ingest, poll every 5s so the badge rolls
  // PENDING → PARSING → … → INDEXED on its own (same cadence as the web console).
  let cloudPollTimer = null;
  function pollWhileWorking() {
    if (cloudPollTimer) { clearInterval(cloudPollTimer); cloudPollTimer = null; }
    const working = () => drive.files.some((f) => RAG_WORKING.has(f.rag_status));
    if (!working()) return;
    cloudPollTimer = setInterval(async () => {
      if (!working()) { clearInterval(cloudPollTimer); cloudPollTimer = null; return; }
      try {
        await loadDrive();
        refreshMain();
      } catch { /* keep polling on transient failures */ }
    }, 5000);
  }

  // ── Sidebar tree (My Drive + workspaces + Trash) ──
  function isCurrentLoc(ws, path, trash) {
    const l = drive.loc;
    if (trash) return l.kind === "trash";
    if (l.kind === "trash") return false;
    if (ws == null) {
      if (l.kind === "root") return path === "";
      return l.ws == null && l.path === path;
    }
    if (l.kind === "workspace") return l.ws === ws && path === "";
    return l.kind === "folder" && l.ws === ws && l.path === path;
  }

  // One top-level scope row (My Drive / a workspace / Trash). Clicking the ▸/▾ toggle
  // expands in place; clicking the row browses it in the main area.
  function topLevelRow(icon, name, ws, path, trash) {
    const row = document.createElement("div");
    row.className = "cd-row cd-folder" + (isCurrentLoc(ws, path, trash) ? " cd-current" : "")
      + (trash ? " cd-trash" : " cd-top");
    row.dataset.path = path;
    row.dataset.ws = trash ? "__trash__" : (ws || "");
    const hasKids = !trash && childrenOf(ws, "").folderKids.length + childrenOf(ws, "").fileKids.length > 0;
    const open = drive.expanded.has(expKey(ws, path));
    const tw = document.createElement("span");
    tw.className = "cd-tw";
    tw.textContent = trash ? "·" : hasKids ? (open ? "▾" : "▸") : "·";
    row.appendChild(tw);
    row.appendChild(Object.assign(document.createElement("span"), { className: "cd-icon", textContent: icon }));
    const nm = document.createElement("span");
    nm.className = "cd-name";
    nm.textContent = name;
    row.appendChild(nm);
    const meta = document.createElement("span");
    meta.className = "cd-meta";
    if (trash) meta.textContent = drive.trash.length ? String(drive.trash.length) : "";
    else if (ws != null) meta.textContent = drive.workspaces.find((x) => x.id === ws)?.role || "";
    row.appendChild(meta);
    row.title = trash ? "Browse Trash" : `Browse ${name}`;
    if (!trash && hasKids) {
      tw.addEventListener("click", (e) => {
        e.stopPropagation();
        if (drive.expanded.has(expKey(ws, path))) drive.expanded.delete(expKey(ws, path));
        else drive.expanded.add(expKey(ws, path));
        renderDrive();
      });
    }
    row.addEventListener("click", () => {
      if (trash) navigate({ kind: "trash" });
      else if (ws == null) navigate({ kind: "root" });
      else navigate({ kind: "workspace", ws });
    });
    return row;
  }

  function renderDrive() {
    cdListEl.innerHTML = "";
    const rootKids = childrenOf(null, "");
    cdListEl.appendChild(topLevelRow("☁️", "My Drive", null, "", false));
    if (drive.expanded.has(expKey(null, ""))) {
      const box = document.createElement("div");
      box.className = "cd-kids";
      renderEntries(null, "", 1, box);
      cdListEl.appendChild(box);
    }
    for (const w of drive.workspaces) {
      cdListEl.appendChild(topLevelRow("📁", w.name, w.id, "", false));
      if (drive.expanded.has(expKey(w.id, ""))) {
        const box = document.createElement("div");
        box.className = "cd-kids";
        renderEntries(w.id, "", 1, box);
        cdListEl.appendChild(box);
      }
    }
    cdListEl.appendChild(topLevelRow("🗑", "Trash", null, "", true));
    if (!drive.workspaces.length && !rootKids.folderKids.length && !rootKids.fileKids.length && !drive.trash.length) {
      const empty = document.createElement("div");
      empty.className = "cd-empty";
      empty.textContent = "My Drive is empty. Right-click or use 📝 / 📁 to add files.";
      cdListEl.appendChild(empty);
    }
    cdPathEl.textContent = locLabel(drive.loc);
    cdPathEl.title = drive.loc.kind === "trash" ? "Trash" : `Browse ${locLabel(drive.loc)}`;
  }

  // Recursively renders folder rows (▸/▾ expandable) then file rows within a scope.
  function renderEntries(ws, path, depth, container) {
    const { folderKids, fileKids } = childrenOf(ws, path);
    if (!folderKids.length && !fileKids.length) {
      const empty = document.createElement("div");
      empty.className = "cd-empty";
      empty.style.padding = "2px 12px";
      empty.textContent = "Empty.";
      container.appendChild(empty);
      return;
    }
    for (const d of folderKids) {
      const { folderKids: kids, fileKids: kf } = childrenOf(ws, d.path);
      const hasKids = kids.length > 0 || kf.length > 0;
      const key = expKey(ws, d.path);
      const open = drive.expanded.has(key);
      const row = document.createElement("div");
      row.className = "cd-row cd-folder" + (isCurrentLoc(ws, d.path, false) ? " cd-current" : "");
      row.dataset.path = d.path;
      row.dataset.ws = ws || "";
      row.dataset.id = d.id || "";
      row.style.paddingLeft = `${6 + depth * 16}px`;
      const tw = document.createElement("span");
      tw.className = "cd-tw";
      tw.textContent = hasKids ? (open ? "▾" : "▸") : "·";
      row.appendChild(tw);
      row.appendChild(Object.assign(document.createElement("span"), { className: "cd-icon", textContent: "📁" }));
      const nm = document.createElement("span");
      nm.className = "cd-name";
      nm.textContent = d.name;
      row.appendChild(nm);
      row.title = `Browse folder ${d.name} (▸ expands in place)`;
      row.draggable = true;
      row.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", JSON.stringify({ kind: "folder", id: d.id || null, path: d.path, ws }));
        e.dataTransfer.effectAllowed = "move";
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", () => row.classList.remove("dragging"));
      tw.addEventListener("click", (e) => {
        e.stopPropagation();
        if (drive.expanded.has(key)) drive.expanded.delete(key);
        else drive.expanded.add(key);
        renderDrive();
      });
      row.addEventListener("click", () => navigate({ kind: "folder", ws, path: d.path }));
      container.appendChild(row);
      if (open) {
        const kidsBox = document.createElement("div");
        kidsBox.className = "cd-kids";
        renderEntries(ws, d.path, depth + 1, kidsBox);
        container.appendChild(kidsBox);
      }
    }
    for (const f of fileKids) {
      const isText = isTextFile(f);
      const row = document.createElement("div");
      row.className = "cd-row cd-file";
      row.style.paddingLeft = `${6 + depth * 16}px`;
      row.dataset.folder = f.folder_path || "";
      row.dataset.ws = ws || "";
      row.dataset.id = f.id;
      row.innerHTML = '<span class="cd-tw"></span>' +
        `<span class="cd-icon">${isText ? "📄" : "📦"}</span>` +
        '<span class="cd-name"></span>' +
        '<span class="cd-meta"></span>';
      row.querySelector(".cd-name").textContent = f.name;
      row.querySelector(".cd-meta").textContent = fmtSize(f.size);
      row.title = isText ? "Open note" : "Open in viewer";
      row.draggable = true;
      row.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", JSON.stringify({ kind: "file", id: f.id, name: f.name, folder_path: f.folder_path || "", ws }));
        e.dataTransfer.effectAllowed = "move";
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", () => row.classList.remove("dragging"));
      row.addEventListener("click", () => { if (isText) openNote(f); else openCloudFile(f); });
      container.appendChild(row);
    }
  }

  // ── Right-click context menu (New text file / New folder / Upload / Delete) ──
  // ctx = { ws, path, file?, folder? } — file/folder are the clicked entity (if any).
  let ctxMenuEl = null;

  function showCtxMenu(x, y, ctx) {
    closeCtxMenu();
    const { ws, path, file, folder } = ctx;
    ctxMenuEl = document.createElement("div");
    ctxMenuEl.className = "drive-ctxmenu";
    const mk = (label, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.addEventListener("click", () => { closeCtxMenu(); fn(); });
      return b;
    };
    const folderLoc = { kind: "folder", ws, path };
    ctxMenuEl.appendChild(mk("📄 New text file", () => createTextFile(folderLoc)));
    ctxMenuEl.appendChild(mk("📁 New folder", () => createFolder(folderLoc)));
    ctxMenuEl.appendChild(mk("📤 Upload file", () => uploadFile(folderLoc)));
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
          if (f.workspace_id !== d.workspace_id) return false;
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

  // ── Drag-and-drop move (scoped: you can't drop across workspaces) ──
  // Rows are draggable (folder → its path, file → its id). Valid drop targets are
  // folder rows / top-level scope rows (move into that scope) and the empty list
  // area (move to the current scope root). File rows are not drop targets.
  function dropTargetFor(e) {
    const folderRow = e.target.closest(".cd-folder");
    if (folderRow) {
      if (folderRow.classList.contains("cd-trash")) return null; // trash isn't a container
      return { el: folderRow, parent: folderRow.dataset.path || "", ws: folderRow.dataset.ws || null };
    }
    if (e.target.closest(".cd-file")) return null;
    return { el: cdListEl, parent: "", ws: drive.loc.kind === "trash" ? null : (drive.loc.ws ?? null) };
  }
  function clearDropTargets() {
    cdListEl.querySelectorAll(".drop-target").forEach((el) => el.classList.remove("drop-target"));
  }
  function dragPayload(e) {
    try { return JSON.parse(e.dataTransfer.getData("text/plain") || "null"); } catch { return null; }
  }
  // The server auto-suffixes busy names ("docs" → "docs(1)"). Surface that to the user.
  function renameHint(requested, final) {
    if (requested && final && requested !== final) {
      Viewer.toast(`"${requested}" already exists — used "${final}" instead.`);
    }
  }

  async function doMove(payload, parent, targetWs) {
    if (!payload) return;
    const srcWs = payload.ws == null ? null : payload.ws;
    if (srcWs !== targetWs) {
      setStatus("Can't move across workspaces.");
      return;
    }
    try {
      if (payload.kind === "file") {
        if ((payload.folder_path || "") === parent) return; // already there
        const res = await apiFetch(`/files/${payload.id}/move`, {
          method: "POST",
          body: JSON.stringify({ workspace_id: srcWs, folder_path: parent || null }),
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
        const row = drive.folders.find((d) => d.path === src && d.workspace_id === srcWs);
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
            if (f.workspace_id !== srcWs) continue;
            const fp = f.folder_path || "";
            if (fp === src || fp.startsWith(src + "/")) {
              const suffix = fp === src ? "" : fp.slice(src.length);
              const res = await apiFetch(`/files/${f.id}/move`, {
                method: "POST",
                body: JSON.stringify({ workspace_id: srcWs, folder_path: (newPath + suffix) || null }),
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
    if (!t) return;
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
    doMove(dragPayload(e), t.parent, t.ws);
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
    doMove(dragPayload(e), "", drive.loc.kind === "trash" ? null : (drive.loc.ws ?? null));
  });

  function isTextFile(f) {
    const name = f.name || "";
    // .csv/.tsv are delimited tables — they open in the SheetJS table viewer, not the
    // note editor, even if the server stored them with a text/* mime type.
    if (/\.(csv|tsv)$/i.test(name)) return false;
    const mime = String(f.mime_type || "").toLowerCase();
    if (mime.startsWith("text/")) return true;
    return /\.(txt|md|markdown|text|log|json|yaml|yml|toml|ini|xml|html|mmd|py|js|ts|jsx|tsx|c|h|cpp|hpp|java|go|rs|sh|bat|sql)$/i.test(name);
  }

  function fmtSize(n) {
    if (n == null) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function fmtDate(s) {
    return s ? new Date(s).toLocaleString() : "—";
  }

  // ── New folder / new text file / upload (scoped to a location) ──
  async function createFolder(loc) {
    if (!getToken()) { Viewer.toast("Sign in to create cloud folders."); return; }
    const name = await window.promptModal({ title: "New folder", placeholder: "Folder name", initial: "" });
    if (!name) return;
    const requested = name.trim();
    const ws = locKindWs(loc);
    const parent = locFolderPath(loc);
    try {
      const res = await apiFetch("/folders", {
        method: "POST",
        body: JSON.stringify({ name: requested, parent_path: parent, workspace_id: ws }),
      });
      if (parent) drive.expanded.add(expKey(ws, parent));
      renameHint(requested, res.name);
      setStatus(`Folder "${res.name}" created.`);
      loadDrive();
    } catch (e) {
      setStatus(`Failed to create folder: ${e.message}`);
    }
  }

  async function createTextFile(loc) {
    if (!getToken()) { Viewer.toast("Sign in to create cloud notes."); return; }
    const name = await window.promptModal({ title: "New text file", placeholder: "File name", initial: "untitled.txt" });
    if (!name) return;
    const content = await window.promptModal({
      title: "Initial content (optional)", placeholder: "Markdown / plain text", initial: "",
      multiline: true, okLabel: "OK",
    });
    if (content === null) return; // cancelled
    const finalName = /\.\w+$/.test(name) ? name : `${name}.txt`;
    const ws = locKindWs(loc);
    const parent = locFolderPath(loc);
    try {
      const bytes = new TextEncoder().encode(content);
      const hex = toHex(await crypto.subtle.digest("SHA-256", bytes));
      const init = await apiFetch("/files/init-upload", {
        method: "POST",
        body: JSON.stringify({
          sha256: hex,
          size: bytes.length,
          name: finalName,
          folder_path: parent,
          workspace_id: ws,
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
      if (parent) drive.expanded.add(expKey(ws, parent));
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

  async function uploadFile(loc) {
    if (!getToken()) { Viewer.toast("Sign in to upload files."); return; }
    const file = await pickLocalFile();
    if (!file) return;
    const ws = locKindWs(loc);
    const parent = locFolderPath(loc);
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
          folder_path: parent,
          workspace_id: ws,
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
      if (parent) drive.expanded.add(expKey(ws, parent));
      renameHint(file.name, created.name);
      setStatus(init.status === "instant"
        ? `Uploaded instantly (deduplicated) "${created.name}".`
        : `Uploaded "${created.name}".`);
      loadDrive();
    } catch (e) {
      setStatus(`Upload failed: ${e.message}`);
    }
  }

  // ── Workspaces ──
  async function createWorkspace() {
    if (!getToken()) { Viewer.toast("Sign in to create workspaces."); return; }
    const name = await window.promptModal({ title: "New workspace", placeholder: "Workspace name", initial: "" });
    if (!name) return;
    try {
      const res = await apiFetch("/workspaces", { method: "POST", body: JSON.stringify({ name: name.trim() }) });
      await loadDrive();
      navigate({ kind: "workspace", ws: res.id });
      setStatus(`Workspace "${res.name || name.trim()}" created.`);
    } catch (e) {
      setStatus(`Failed to create workspace: ${e.message}`);
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
      viewerCloudFile = f; // so "attach to chat" can reference this cloud asset
      Viewer.render(res.path, f.name);
    } catch (e) {
      setStatus(`Failed to open "${f.name}": ${e.message}`);
    }
  }

  // ── Note editor ──
  // Icon buttons carry an SVG + a label span; only the label changes at runtime.
  function noteBtn(btn, text) {
    const el = btn.querySelector(".note-btn-label");
    if (el) el.textContent = text; else btn.textContent = text;
  }

  async function openNote(f) {
    if (!getToken()) { Viewer.toast("Sign in to open cloud notes."); return; }
    if (note.dirty && !(await window.confirmModal("Discard unsaved changes to the current note?"))) return;
    setStatus("Loading note…");
    try {
      viewerCloudFile = null; // the note editor replaces the viewer as the active surface
      const res = await apiFetch(`/files/${f.id}/content`);
      note.asset = f;
      note.dirty = false;
      note.preview = false;
      noteTextarea.value = res.content || "";
      noteTitleEl.textContent = f.folder_path ? `${f.folder_path}/${f.name}` : f.name;
      setPreviewMode(false);
      noteSaveBtn.disabled = true;
      noteBtn(noteSaveBtn, "Save");
      noteEditor.classList.remove("hidden");
      setStatus("");
      // A Mermaid mindmap opens straight into the diagram preview (树状图) instead of raw
      // indented text; ✏️ Edit still shows the .mmd source.
      if (/^\s*mindmap\b/.test(res.content || "")) setPreviewMode(true);
      else noteTextarea.focus();
    } catch (e) {
      setStatus(`Failed to open note: ${e.message}`);
    }
  }

  async function saveNote() {
    if (!note.asset) return;
    noteSaveBtn.disabled = true;
    noteBtn(noteSaveBtn, "Saving…");
    try {
      const res = await apiFetch(`/files/${note.asset.id}/content`, {
        method: "PUT",
        body: JSON.stringify({ content: noteTextarea.value }),
      });
      note.asset = { ...note.asset, size: res.asset?.size ?? note.asset.size, updated_at: res.asset?.updated_at ?? note.asset.updated_at };
      note.dirty = false;
      noteBtn(noteSaveBtn, "Saved ✓");
      setTimeout(() => { noteBtn(noteSaveBtn, "Save"); }, 1500);
      loadDrive(); // refresh size/updated_at in the sidebar list
      setStatus("Note saved. Re-indexing in background…");
    } catch (e) {
      noteSaveBtn.disabled = false;
      noteBtn(noteSaveBtn, "Save");
      setStatus(`Save failed: ${e.message}`);
    }
  }

  function setPreviewMode(on) {
    note.preview = on;
    notePreviewPane.classList.toggle("hidden", !on);
    noteTextarea.classList.toggle("hidden", on);
    noteModeEl.textContent = on ? "Preview" : "Edit";
    noteBtn(notePreviewBtn, on ? "Edit" : "Preview");
    if (on) {
      notePreviewPane.innerHTML = /^\s*mindmap\b/.test(noteTextarea.value)
        ? renderMindmap(noteTextarea.value)
        : renderMarkdown(noteTextarea.value);
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
    noteBtn(noteSaveBtn, "Save");
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

  // ── Mermaid mindmap preview: parse the indented tree and draw an SVG diagram ──
  // Toolkit mindmaps are saved as Mermaid `mindmap` syntax (indent = nesting depth):
  //   mindmap
  //     root((Topic))
  //       Branch
  //         Detail
  // Markdown would only render flat indented lines, so we rebuild the tree and lay it out
  // as a tidy-tree SVG — the note preview then shows an actual 树状图.
  function mmTextWidth(label) {
    const s = String(label || "");
    let w = 0;
    for (const ch of s) w += ch.charCodeAt(0) > 0x2e7f ? 13 : 7; // CJK ≈ 13px, ASCII ≈ 7px @12px font
    return w + 20;
  }
  function mmUnquote(label) {
    const s = String(label).trim();
    return s.length >= 2 && s.startsWith('"') && s.endsWith('"') ? s.slice(1, -1) : s;
  }
  function parseMindmap(text) {
    const nodes = [];
    for (const raw of String(text || "").split(/\r?\n/)) {
      const line = raw.replace(/%%.*$/, "").replace(/\r$/, "");
      if (!line.trim()) continue;
      const indent = line.length - line.trimStart().length;
      const token = line.trim();
      if (token === "mindmap") continue;
      if (token.startsWith("root")) {
        let inner = token.slice("root".length).replace(/^\s*\(\s*/, "").replace(/\s*\)\s*$/, "");
        while (inner.startsWith("(") && inner.endsWith(")")) inner = inner.slice(1, -1);
        nodes.push({ indent, label: mmUnquote(inner) || "Mind Map", root: true });
      } else {
        nodes.push({ indent, label: mmUnquote(token), root: false });
      }
    }
    if (!nodes.length) return null;
    const root = { label: nodes[0].label || "Mind Map", children: [] };
    const stack = [{ node: root, indent: nodes[0].indent }];
    for (let i = 1; i < nodes.length; i++) {
      const n = nodes[i];
      while (stack.length > 1 && n.indent <= stack[stack.length - 1].indent) stack.pop();
      const child = { label: n.label, children: [] };
      stack[stack.length - 1].node.children.push(child);
      stack.push({ node: child, indent: n.indent });
    }
    return root;
  }
  // Horizontal (root-on-left) layout: branches grow rightwards, siblings stack down, so a
  // dense tree stays narrow instead of spreading too wide to read. Depth → x, subtree → y.
  const MM_H_GAP = 56;   // horizontal gap between a parent's right edge and a child's left edge
  const MM_V_GAP = 40;   // vertical gap between siblings (≥ node height so a parent box fits)
  const MM_NODE_H = 32;  // node box height
  function mmNodeBoxW(label) {
    return Math.max(mmTextWidth(label), 48);
  }
  function mmSubtreeHeight(n) {
    if (n._sh != null) return n._sh;
    if (!n.children.length) return (n._sh = MM_NODE_H);
    const kids = n.children.reduce((s, c) => s + mmSubtreeHeight(c), 0) + MM_V_GAP * (n.children.length - 1);
    return (n._sh = Math.max(MM_NODE_H, kids));
  }
  function mmLayout(n, px, y0, depth) {
    n.depth = depth;
    n._w = mmNodeBoxW(n.label);
    n.x = px + n._w / 2;
    n.y = y0 + mmSubtreeHeight(n) / 2;
    const nextX = px + n._w + MM_H_GAP;
    let cy = y0;
    for (const c of n.children) {
      mmLayout(c, nextX, cy, depth + 1);
      cy += mmSubtreeHeight(c) + MM_V_GAP;
    }
  }
  function mmSvgEscape(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function renderMindmap(text) {
    const tree = parseMindmap(text);
    if (!tree) return '<p class="cd-hint">Empty or unsupported mind map.</p>';
    mmLayout(tree, 0, 0, 0);
    const H = Math.max(mmSubtreeHeight(tree), 200);
    let W = 0;
    (function walk(n) {
      W = Math.max(W, n.x + n._w / 2);
      n.children.forEach(walk);
    })(tree);
    W = Math.max(W, 320);
    const pad = 24;
    const parts = [
      `<svg class="mmd-tree" width="${Math.ceil(W + pad * 2)}" height="${Math.ceil(H + pad * 2)}" viewBox="-${pad} -${pad} ${W + pad * 2} ${H + pad * 2}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="mind map">`,
    ];
    const edges = [];
    (function walk(n) {
      for (const c of n.children) {
        const sx = n.x + n._w / 2;
        const ex = c.x - c._w / 2;
        edges.push(`M ${sx} ${n.y} C ${sx + MM_H_GAP / 2} ${n.y}, ${ex - MM_H_GAP / 2} ${c.y}, ${ex} ${c.y}`);
      }
      n.children.forEach(walk);
    })(tree);
    parts.push(`<g fill="none" stroke="#a8bce0" stroke-width="1.5"><path d="${edges.join(" ")}"/></g>`);
    (function walk(n) {
      if (n.depth === 0) {
        parts.push(
          `<rect x="${n.x - n._w / 2}" y="${n.y - MM_NODE_H / 2}" width="${n._w}" height="${MM_NODE_H}" rx="10" fill="#4f8cff" stroke="#3a6fd0" stroke-width="1.5"/>`,
          `<text x="${n.x}" y="${n.y}" fill="#fff" font-size="13" font-weight="700" text-anchor="middle" dominant-baseline="central">${mmSvgEscape(n.label)}</text>`
        );
      } else {
        parts.push(
          `<rect x="${n.x - n._w / 2}" y="${n.y - MM_NODE_H / 2}" width="${n._w}" height="${MM_NODE_H}" rx="10" fill="#ffffff" stroke="#c8d6f0" stroke-width="1"/>`,
          `<text x="${n.x}" y="${n.y}" fill="#33415f" font-size="12" text-anchor="middle" dominant-baseline="central">${mmSvgEscape(n.label)}</text>`
        );
      }
      n.children.forEach(walk);
    })(tree);
    parts.push("</svg>");
    return parts.join("\n");
  }

  // ── Main-area handlers + batch operations ──
  function onMainAction(name) {
    switch (name) {
      case "view-list": drive.viewMode = "list"; rerenderMainPreserve(); break;
      case "view-grid": drive.viewMode = "grid"; rerenderMainPreserve(); break;
      case "edit": toggleEditMode(); break;
      case "manage": openManageModal(); break;
      case "new-folder": createFolder(drive.loc); break;
      case "new-text": createTextFile(drive.loc); break;
      case "upload": uploadFile(drive.loc); break;
      case "empty-trash": emptyTrash(); break;
    }
  }

  function onBatch(name) {
    switch (name) {
      case "download": downloadSelected(); break;
      case "open": openSelected(); break;
      case "share": {
        const f = selectedFilesArr()[0];
        if (f) openShareModal(f);
        break;
      }
      case "rename": renameTarget(); break;
      case "move": moveTargets(); break;
      case "delete": deleteSelected(); break;
      case "restore": restoreSelected(); break;
      case "purge": purgeSelected(); break;
    }
  }

  function toggleOne(id) {
    const s = new Set(drive.selected);
    if (s.has(id)) s.delete(id); else s.add(id);
    drive.selected = s;
    rerenderMainPreserve();
  }
  function toggleAll(checked) {
    const ids = currentLocFiles().map((f) => f.id);
    drive.selected = checked ? new Set(ids) : new Set();
    rerenderMainPreserve();
  }
  function selectedFilesArr() {
    return currentLocFiles().filter((f) => drive.selected.has(f.id));
  }

  async function downloadFile(f) {
    try {
      const headers = { Authorization: `Bearer ${getToken()}` };
      const res = await fetch(`/api/files/${f.id}/download`, { headers });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = f.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setStatus(`Download failed: ${e.message}`);
    }
  }
  async function downloadSelected() {
    for (const f of selectedFilesArr()) await downloadFile(f);
  }
  async function openSelected() {
    const f = selectedFilesArr()[0];
    if (!f) return;
    if (isTextFile(f)) openNote(f); else openCloudFile(f);
  }
  async function renameTarget() {
    const f = selectedFilesArr()[0];
    if (!f) return;
    const name = await window.promptModal({ title: "Rename file", placeholder: "File name", initial: f.name });
    if (!name) return;
    try {
      const res = await apiFetch(`/files/${f.id}`, {
        method: "PATCH",
        body: JSON.stringify({ name: name.trim(), folder_path: f.folder_path }),
      });
      renameHint(f.name, res.name);
      setStatus(`Renamed to "${res.name}".`);
      loadDrive();
    } catch (e) {
      setStatus(`Rename failed: ${e.message}`);
    }
  }
  async function moveTargets() {
    const files = selectedFilesArr();
    if (!files.length) return;
    openMoveModal(files);
  }
  async function deleteSelected() {
    const fs = selectedFilesArr();
    if (!fs.length) return;
    const ok = await window.confirmModal({
      title: "Move to Trash?",
      message: `Move ${fs.length} file${fs.length > 1 ? "s" : ""} to Trash?\nThey can be restored later.`,
      okLabel: "Delete",
    });
    if (!ok) return;
    try {
      for (const f of fs) await apiFetch(`/files/${f.id}`, { method: "DELETE" });
      drive.selected = new Set();
      setStatus(`Moved ${fs.length} file${fs.length > 1 ? "s" : ""} to Trash.`);
      loadDrive();
    } catch (e) {
      setStatus(`Delete failed: ${e.message}`);
    }
  }
  async function restoreSelected() {
    const fs = selectedFilesArr();
    if (!fs.length) return;
    try {
      for (const f of fs) await apiFetch(`/trash/${f.id}/restore`, { method: "POST" });
      drive.selected = new Set();
      setStatus(`Restored ${fs.length} file${fs.length > 1 ? "s" : ""}.`);
      loadDrive();
    } catch (e) {
      setStatus(`Restore failed: ${e.message}`);
    }
  }
  async function purgeSelected() {
    const fs = selectedFilesArr();
    if (!fs.length) return;
    const ok = await window.confirmModal({
      title: "Delete permanently?",
      message: `Permanently delete ${fs.length} file${fs.length > 1 ? "s" : ""}?\nThis cannot be undone.`,
      okLabel: "Delete permanently",
      okClass: "primary",
    });
    if (!ok) return;
    try {
      for (const f of fs) await apiFetch(`/trash/${f.id}`, { method: "DELETE" });
      drive.selected = new Set();
      setStatus(`Permanently deleted ${fs.length} file${fs.length > 1 ? "s" : ""}.`);
      loadDrive();
    } catch (e) {
      setStatus(`Purge failed: ${e.message}`);
    }
  }
  async function emptyTrash() {
    const n = drive.trash.length;
    if (!n) { Viewer.toast("Trash is empty."); return; }
    const ok = await window.confirmModal({
      title: "Empty Trash?",
      message: `Permanently delete all ${n} file${n > 1 ? "s" : ""} in Trash?\nThis cannot be undone.`,
      okLabel: "Empty Trash",
      okClass: "primary",
    });
    if (!ok) return;
    try {
      await apiFetch("/trash", { method: "DELETE" });
      setStatus("Trash emptied.");
      loadDrive();
    } catch (e) {
      setStatus(`Empty Trash failed: ${e.message}`);
    }
  }

  // ── Modals (Move / Share / Workspace manage) ──
  // A small overlay builder reusing the app's .overlay/.modal styles; modals are
  // dismissed via the ✕, an outside click, or the supplied close().
  function openModal(title, buildBody) {
    const overlay = document.createElement("div");
    overlay.className = "overlay";
    const modal = document.createElement("div");
    modal.className = "modal";
    const header = document.createElement("div");
    header.className = "modal-header";
    const h3 = document.createElement("h3");
    h3.textContent = title;
    const closeBtn = document.createElement("button");
    closeBtn.className = "modal-close";
    closeBtn.textContent = "✕";
    closeBtn.title = "Close";
    closeBtn.onclick = () => overlay.remove();
    header.append(h3, closeBtn);
    modal.appendChild(header);
    const body = buildBody();
    modal.appendChild(body);
    overlay.appendChild(modal);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
    return { overlay, modal, close: () => overlay.remove() };
  }

  function openMoveModal(files) {
    const ws = drive.loc.kind === "trash" ? null : drive.loc.ws;
    const path = drive.loc.kind === "folder" ? drive.loc.path : "";
    const { close } = openModal(files.length > 1 ? `Move ${files.length} files` : "Move file", () => {
      const body = document.createElement("div");
      body.className = "cdt-modal-body";
      const wsLabel = document.createElement("label");
      wsLabel.textContent = "Destination workspace";
      const wsSel = document.createElement("select");
      const rootOpt = document.createElement("option");
      rootOpt.value = "";
      rootOpt.textContent = "My Drive";
      wsSel.appendChild(rootOpt);
      for (const w of drive.workspaces) {
        const o = document.createElement("option");
        o.value = w.id;
        o.textContent = w.name;
        if (w.id === ws) o.selected = true;
        wsSel.appendChild(o);
      }
      wsLabel.appendChild(wsSel);
      const pathLabel = document.createElement("label");
      pathLabel.textContent = "Folder path (within workspace; empty = root)";
      const pathInput = document.createElement("input");
      pathInput.value = path;
      pathInput.placeholder = "e.g. English/Vocab";
      pathLabel.appendChild(pathInput);
      const err = document.createElement("p");
      err.className = "cfg-status";
      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.onclick = close;
      const save = document.createElement("button");
      save.type = "button";
      save.className = "primary";
      save.textContent = "Move";
      save.onclick = async () => {
        err.textContent = "";
        try {
          const workspace_id = wsSel.value ? wsSel.value : null;
          const folder_path = pathInput.value.trim() ? pathInput.value.trim() : null;
          for (const f of files) {
            await apiFetch(`/files/${f.id}/move`, {
              method: "POST",
              body: JSON.stringify({ workspace_id, folder_path }),
            });
          }
          close();
          setStatus(`Moved ${files.length} file${files.length > 1 ? "s" : ""}.`);
          loadDrive();
        } catch (e) {
          err.textContent = e.message || String(e);
          err.className = "cfg-status err";
        }
      };
      actions.append(cancel, save);
      body.append(wsLabel, pathLabel, err, actions);
      return body;
    });
  }

  function openShareModal(f) {
    const { close } = openModal(`Share "${f.name}"`, () => {
      const body = document.createElement("div");
      body.className = "cdt-modal-body";
      const err = document.createElement("p");
      err.className = "cfg-status";
      const listBox = document.createElement("div");
      listBox.className = "cdt-share-list";

      const load = async () => {
        try {
          const r = await apiFetch(`/files/${f.id}/shares`);
          listBox.innerHTML = "";
          const shares = r.shares || [];
          if (!shares.length) {
            const none = document.createElement("div");
            none.className = "cdt-muted";
            none.textContent = "No shares yet.";
            listBox.appendChild(none);
            return;
          }
          for (const s of shares) {
            const row = document.createElement("div");
            row.className = "cdt-share-row";
            const name = document.createElement("span");
            name.className = "cdt-share-name";
            name.textContent = s.grantee_user_id ? `👤 ${s.grantee_user_id}` : "🌍 Public";
            row.appendChild(name);
            const perm = document.createElement("span");
            perm.className = "cdt-share-perm";
            perm.textContent = s.permission;
            row.appendChild(perm);
            const revoke = document.createElement("button");
            revoke.type = "button";
            revoke.className = "cdt-btn cdt-danger";
            revoke.textContent = "Revoke";
            revoke.onclick = async () => {
              try {
                await apiFetch(`/files/${f.id}/share/${s.grantee_user_id ? s.grantee_user_id : "public"}`, { method: "DELETE" });
                await load();
              } catch (e) { err.textContent = e.message || String(e); err.className = "cfg-status err"; }
            };
            row.appendChild(revoke);
            listBox.appendChild(row);
          }
        } catch (e) {
          err.textContent = e.message || String(e);
          err.className = "cfg-status err";
        }
      };

      // Public link toggle
      const pubLabel = document.createElement("label");
      pubLabel.className = "cdt-share-public";
      const pubCb = document.createElement("input");
      pubCb.type = "checkbox";
      const pubSpan = document.createElement("span");
      pubSpan.textContent = "Public link (any signed-in user)";
      pubLabel.append(pubCb, pubSpan);
      // Grantee + permission + Add
      const addRow = document.createElement("div");
      addRow.className = "cdt-share-add";
      const grantee = document.createElement("input");
      grantee.type = "text";
      grantee.placeholder = "User UUID";
      const permSel = document.createElement("select");
      ["read", "write"].forEach((p) => {
        const o = document.createElement("option");
        o.value = p;
        o.textContent = p;
        permSel.appendChild(o);
      });
      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "cdt-btn primary";
      addBtn.textContent = "Add";
      addBtn.onclick = async () => {
        err.textContent = "";
        try {
          await apiFetch(`/files/${f.id}/share`, {
            method: "POST",
            body: JSON.stringify({ grantee_user_id: pubCb.checked ? null : (grantee.value.trim() || null), permission: permSel.value }),
          });
          grantee.value = "";
          err.className = "cfg-status ok";
          err.textContent = pubCb.checked ? "Public link created." : "Share saved.";
          await load();
        } catch (e) {
          err.className = "cfg-status err";
          err.textContent = e.message || String(e);
        }
      };
      pubCb.addEventListener("change", () => {
        grantee.style.display = pubCb.checked ? "none" : "";
      });
      addRow.append(grantee, permSel, addBtn);
      body.append(pubLabel, addRow, err, listBox);
      load();
      return body;
    });
  }

  // Workspace manage modal: members (add / role / remove), activity log, and
  // owner-only rename + delete.
  function openManageModal() {
    const wsId = drive.loc.kind === "trash" ? null : drive.loc.ws;
    if (!wsId) return;
    const w = drive.workspaces.find((x) => x.id === wsId);
    if (!w) return;
    const isOwner = !!(drive.me && drive.me.user_id === w.owner_id);
    const canManageMembers = isOwner || w.role === "admin";
    const ownerName = w.owner_display_name || w.owner_username || w.owner_id;

    const { close } = openModal(`⚙ ${w.name}`, () => {
      const body = document.createElement("div");
      body.className = "cdt-manage-body";

      const tabs = document.createElement("div");
      tabs.className = "cdt-manage-tabs";
      const panel = document.createElement("div");
      panel.className = "cdt-manage-panel";

      const membersTab = document.createElement("button");
      membersTab.type = "button";
      membersTab.className = "cdt-manage-tab";
      membersTab.textContent = "Members";
      const logsTab = document.createElement("button");
      logsTab.type = "button";
      logsTab.className = "cdt-manage-tab";
      logsTab.textContent = "Activity";
      tabs.append(membersTab, logsTab);

      const settings = document.createElement("div");
      settings.className = "cdt-manage-settings";
      const renameInput = document.createElement("input");
      renameInput.value = w.name;
      renameInput.placeholder = "Workspace name";
      const renameBtn = document.createElement("button");
      renameBtn.type = "button";
      renameBtn.className = "cdt-btn";
      renameBtn.textContent = "Rename";
      renameBtn.disabled = !isOwner;
      renameBtn.title = isOwner ? "Rename workspace" : "Only the owner can rename this workspace";
      renameBtn.onclick = async () => {
        const name = renameInput.value.trim();
        if (!name) return;
        try {
          await apiFetch(`/workspaces/${wsId}`, { method: "PATCH", body: JSON.stringify({ name }) });
          await loadDrive();
          setStatus(`Workspace renamed to "${name}".`);
          close();
        } catch (e) { Viewer.toast(`Rename failed: ${e.message}`); }
      };
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "cdt-btn cdt-danger";
      delBtn.textContent = "Delete workspace";
      delBtn.disabled = !isOwner;
      delBtn.title = isOwner ? "Permanently delete this workspace" : "Only the owner can delete this workspace";
      delBtn.onclick = async () => {
        const ok = await window.confirmModal({
          title: "Delete workspace?",
          message: `Permanently delete workspace "${w.name}" and all files in it?\nThis cannot be undone.`,
          okLabel: "Delete workspace",
        });
        if (!ok) return;
        try {
          await apiFetch(`/workspaces/${wsId}`, { method: "DELETE" });
          await loadDrive();
          if (drive.loc.kind === "workspace" || drive.loc.ws === wsId) navigate({ kind: "root" });
          close();
        } catch (e) { Viewer.toast(`Delete failed: ${e.message}`); }
      };
      settings.append(renameInput, renameBtn, delBtn);
      body.append(tabs, panel, settings);

      let active = "members";
      const showTab = () => {
        panel.innerHTML = "";
        if (active === "members") renderMembers(panel);
        else renderLogs(panel);
      };
      membersTab.onclick = () => {
        membersTab.classList.add("active");
        logsTab.classList.remove("active");
        active = "members";
        showTab();
      };
      logsTab.onclick = () => {
        logsTab.classList.add("active");
        membersTab.classList.remove("active");
        active = "logs";
        showTab();
      };
      membersTab.classList.add("active");
      showTab();
      return body;
    });

    function renderMembers(panel) {
      const addRow = document.createElement("div");
      addRow.className = "cdt-manage-add";
      const userInput = document.createElement("input");
      userInput.placeholder = "Search users…";
      const roleSel = document.createElement("select");
      ["viewer", "editor", "admin"].forEach((r) => {
        const o = document.createElement("option");
        o.value = r;
        o.textContent = r;
        roleSel.appendChild(o);
      });
      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "cdt-btn primary";
      addBtn.textContent = "Add";
      addBtn.disabled = !canManageMembers;
      addBtn.title = canManageMembers ? "Add member" : "Only the owner or an admin can add members";
      const sugBox = document.createElement("div");
      sugBox.className = "cdt-user-sug hidden";
      let picked = null;
      let sugTimer = null;
      userInput.addEventListener("input", () => {
        clearTimeout(sugTimer);
        const q = userInput.value.trim();
        if (!q) { picked = null; sugBox.classList.add("hidden"); return; }
        sugTimer = setTimeout(async () => {
          try {
            const r = await apiFetch(`/users/search?q=${encodeURIComponent(q)}&limit=8`);
            const users = r.users || [];
            sugBox.innerHTML = "";
            if (!users.length) { sugBox.classList.add("hidden"); return; }
            for (const u of users) {
              const item = document.createElement("div");
              item.className = "cdt-user-sug-item";
              item.textContent = `${u.display_name || u.username || u.user_id} (${u.username || String(u.user_id).slice(0, 8)})`;
              item.addEventListener("mousedown", (e) => {
                e.preventDefault();
                picked = u;
                userInput.value = u.display_name || u.username || u.user_id;
                sugBox.classList.add("hidden");
              });
              sugBox.appendChild(item);
            }
            sugBox.classList.remove("hidden");
          } catch { /* ignore transient errors */ }
        }, 200);
      });
      addBtn.onclick = async () => {
        if (!picked) { Viewer.toast("Pick a matching user from the suggestions."); return; }
        try {
          await apiFetch(`/workspaces/${wsId}/members`, {
            method: "POST",
            body: JSON.stringify({ user_id: picked.user_id, role: roleSel.value }),
          });
          userInput.value = "";
          picked = null;
          renderMembers(panel); // re-render to show the new row
        } catch (e) { Viewer.toast(`Add failed: ${e.message}`); }
      };
      addRow.append(userInput, roleSel, addBtn);
      panel.appendChild(addRow);
      panel.appendChild(sugBox);

      const mkMemberRow = (m, isOwnerRow) => {
        const row = document.createElement("div");
        row.className = "cdt-member-row";
        const name = document.createElement("span");
        name.className = "cdt-member-name";
        name.textContent = m.display_name || m.username || m.user_id;
        name.title = m.user_id;
        row.appendChild(name);
        const roleBadge = document.createElement("span");
        roleBadge.className = "cdt-member-role " + m.role;
        roleBadge.textContent = m.role;
        row.appendChild(roleBadge);
        if (isOwnerRow) return row;
        const sel = document.createElement("select");
        ["viewer", "editor", "admin"].forEach((r) => {
          const o = document.createElement("option");
          o.value = r;
          o.textContent = r;
          if (r === m.role) o.selected = true;
          sel.appendChild(o);
        });
        sel.disabled = !canManageMembers || (m.role === "admin" && !isOwner);
        sel.title = sel.disabled ? (isOwner ? "Only the owner can change an admin's role" : "Only the owner or an admin can change roles") : "Change role";
        sel.addEventListener("change", async () => {
          try {
            await apiFetch(`/workspaces/${wsId}/members/${m.user_id}`, { method: "PATCH", body: JSON.stringify({ role: sel.value }) });
            m.role = sel.value;
            roleBadge.textContent = m.role;
            roleBadge.className = "cdt-member-role " + m.role;
          } catch (e) {
            sel.value = m.role;
            Viewer.toast(`Role update failed: ${e.message}`);
          }
        });
        row.appendChild(sel);
        const rmBtn = document.createElement("button");
        rmBtn.type = "button";
        rmBtn.className = "cdt-btn cdt-danger";
        rmBtn.textContent = "Remove";
        rmBtn.disabled = !canManageMembers || (m.role === "admin" && !isOwner);
        rmBtn.title = rmBtn.disabled ? "Only the owner can remove an admin" : "Remove member";
        rmBtn.onclick = async () => {
          const ok = await window.confirmModal({
            title: "Remove member?",
            message: `Remove ${m.display_name || m.username || m.user_id} from this workspace?`,
            okLabel: "Remove",
          });
          if (!ok) return;
          try {
            await apiFetch(`/workspaces/${wsId}/members/${m.user_id}`, { method: "DELETE" });
            row.remove();
          } catch (e) { Viewer.toast(`Remove failed: ${e.message}`); }
        };
        row.appendChild(rmBtn);
        return row;
      };

      panel.appendChild(mkMemberRow(
        { user_id: w.owner_id, username: w.owner_username, display_name: w.owner_display_name, role: "owner" },
        true
      ));
      apiFetch(`/workspaces/${wsId}/members`)
        .then((r) => {
          for (const m of (r.members || [])) panel.appendChild(mkMemberRow(m, false));
        })
        .catch((e) => Viewer.toast(`Failed to load members: ${e.message}`));
    }

    function renderLogs(panel) {
      const filters = document.createElement("div");
      filters.className = "cdt-manage-log-filters";
      const qInput = document.createElement("input");
      qInput.placeholder = "Search actor / target…";
      const startInput = document.createElement("input");
      startInput.type = "text";
      startInput.placeholder = "Start YYYY-MM-DD";
      const endInput = document.createElement("input");
      endInput.type = "text";
      endInput.placeholder = "End YYYY-MM-DD";
      const goBtn = document.createElement("button");
      goBtn.type = "button";
      goBtn.className = "cdt-btn primary";
      goBtn.textContent = "Search";
      const err = document.createElement("p");
      err.className = "cfg-status";
      const listBox = document.createElement("div");
      listBox.className = "cdt-log-list";
      filters.append(qInput, startInput, endInput, goBtn);
      panel.append(filters, err, listBox);

      const load = async () => {
        const params = new URLSearchParams({ limit: "50" });
        if (qInput.value.trim()) params.set("q", qInput.value.trim());
        if (startInput.value.trim()) params.set("start", startInput.value.trim());
        if (endInput.value.trim()) params.set("end", endInput.value.trim());
        try {
          const r = await apiFetch(`/workspaces/${wsId}/activity?${params.toString()}`);
          listBox.innerHTML = "";
          const items = r.items || [];
          if (!items.length) {
            const none = document.createElement("div");
            none.className = "cdt-muted";
            none.textContent = "No activity yet.";
            listBox.appendChild(none);
            return;
          }
          for (const it of items) {
            const row = document.createElement("div");
            row.className = "cdt-log-row";
            const head = document.createElement("div");
            head.className = "cdt-log-head";
            const who = document.createElement("span");
            who.className = "cdt-log-who";
            who.textContent = it.actor_username || "system";
            head.appendChild(who);
            const when = document.createElement("span");
            when.className = "cdt-log-when";
            when.textContent = fmtDate(it.created_at);
            head.appendChild(when);
            const action = document.createElement("div");
            action.className = "cdt-log-action";
            action.textContent = it.action + (it.target_name ? ` · ${it.target_name}` : "");
            row.append(head, action);
            listBox.appendChild(row);
          }
        } catch (e) {
          err.textContent = e.message || String(e);
          err.className = "cfg-status err";
        }
      };
      goBtn.onclick = load;
      load();
    }
  }

  // ── Search (client-side fuzzy autocomplete, sidebar) ──
  // All files/folders across every scope are loaded, so suggestions are computed
  // locally while typing — same scoring as the web console. Clicking a suggestion
  // reveals that entry in the tree (expands its ancestors) and flashes it.
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
      if (score != null) {
        scored.push({ score, item: { kind: "file", f, name: f.name, path: f.folder_path || "", ws: f.workspace_id } });
      }
    }
    for (const d of allFolders()) {
      const n = fuzzyScore(d.name, q);
      const p = fuzzyScore(d.path, q);
      const score = n != null ? n : p != null ? 1000 + p : null;
      if (score != null) {
        scored.push({ score, item: { kind: "folder", d, name: d.name, path: d.path, ws: d.workspace_id } });
      }
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
      meta.textContent = it.ws ? `${wsName(it.ws)}${it.path ? " / " + it.path : ""}` : it.path || "";
      row.append(icon, name, meta);
      row.addEventListener("mousedown", (e) => {
        e.preventDefault();
        if (it.kind === "file") jumpToFile(it.f); else jumpToFolder(it.d);
      });
      cdSuggest.appendChild(row);
    }
    cdSuggest.classList.remove("hidden");
  }

  // Expand the workspace scope + every ancestor folder of `path` so a search hit
  // inside it becomes visible.
  function expandAncestors(ws, path) {
    drive.expanded.add(expKey(ws, ""));
    let cur = "";
    for (const part of String(path || "").split("/")) {
      if (!part) continue;
      cur = cur ? `${cur}/${part}` : part;
      drive.expanded.add(expKey(ws, cur));
    }
  }

  function findRow(pred) {
    return Array.from(cdListEl.querySelectorAll(".cd-row")).find(pred) || null;
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
    expandAncestors(f.workspace_id, f.folder_path);
    renderDrive();
    revealRow(findRow((r) => r.dataset.id === f.id));
    clearSearch();
  }

  function jumpToFolder(d) {
    expandAncestors(d.workspace_id, d.path);
    renderDrive();
    revealRow(findRow((r) => r.dataset.ws === (d.workspace_id || "") && r.dataset.path === d.path));
    clearSearch();
  }

  // Wire the sidebar search box (input + clear + keyboard nav + outside-click close).
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
  cdNewFolder.addEventListener("click", () => createFolder(drive.loc));
  cdNewText.addEventListener("click", () => createTextFile(drive.loc));
  if (cdNewWs) cdNewWs.addEventListener("click", createWorkspace);
  cdListEl.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    // Right-click a folder → create inside it / delete it; a file → create in its
    // folder / delete it; empty area → create at the current scope root.
    const folderRow = e.target.closest(".cd-folder");
    const fileRow = e.target.closest(".cd-file");
    const scopeWs = drive.loc.kind === "trash" ? null : drive.loc.ws;
    const ctx = { ws: scopeWs, path: "", file: null, folder: null };
    if (folderRow) {
      const p = folderRow.dataset.path;
      const ws = folderRow.dataset.ws === "__trash__" ? null : (folderRow.dataset.ws || null);
      ctx.ws = ws;
      ctx.path = p;
      ctx.folder = drive.folders.find((d) => d.path === p && d.workspace_id === ws)
        || { name: p ? p.split("/").pop() : "", path: p, workspace_id: ws };
    } else if (fileRow) {
      ctx.path = fileRow.dataset.folder || "";
      ctx.ws = fileRow.dataset.ws || null;
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
    // Esc closes the note editor when open.
    if (e.key === "Escape" && !noteEditor.classList.contains("hidden")) {
      e.preventDefault();
      closeNote();
    }
  });

  // ── "Choose a Cloud Drive folder" picker (used by session → toolkit generation and the
  // generate dialog's output-folder picker) ──
  // A save-dialog-style folder tree that reuses the sidebar's row/kids markup and the
  // same expansion state, so it looks and behaves like the upload panel. Only My Drive
  // (personal, workspace_id null) folders are offered — session artifacts always land in
  // the caller's personal drive. Resolves with the chosen folder path (null = My Drive
  // root) or undefined when cancelled. With ``prompt: false`` the prompt textarea is
  // omitted (pure folder selection, ok label overridable via ``okLabel``).
  window.pickDriveFolderModal = async (title, { defaultPrompt = "", prompt = true, okLabel = "Generate" } = {}) => {
    if (!getToken()) { Viewer.toast("Sign in to choose a cloud folder."); return undefined; }
    try { await loadDrive(); } catch { /* keep whatever folders we already have */ }

    const overlay = document.createElement("div");
    overlay.className = "overlay";
    const modal = document.createElement("div");
    modal.className = "modal cd-picker-modal";

    const header = document.createElement("div");
    header.className = "modal-header";
    const h = document.createElement("h3");
    h.textContent = title;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "modal-close";
    close.innerHTML = "&times;";
    close.title = "Close";
    header.append(h, close);

    const target = document.createElement("div");
    target.className = "cd-picker-target";

    const tree = document.createElement("div");
    tree.className = "cd-picker-tree";

    const promptEl = prompt ? document.createElement("textarea") : null;
    if (promptEl) {
      promptEl.className = "cd-prompt-input cd-picker-prompt";
      promptEl.rows = 6;
      promptEl.placeholder = defaultPrompt || "Optional: write your own requirements here…";
    }

    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "Cancel";
    const okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "primary";
    okBtn.textContent = okLabel;
    actions.append(cancelBtn, okBtn);

    modal.append(header, target, tree);
    if (promptEl) modal.append(promptEl);
    modal.append(actions);
    overlay.appendChild(modal);

    let selected = null; // folder path; null = My Drive root
    let resolveVal;
    const promise = new Promise((res) => { resolveVal = res; });

    function finish(val) {
      document.removeEventListener("keydown", onEsc);
      overlay.remove();
      resolveVal(val);
    }

    function label() {
      return selected ? `My Drive / ${selected.split("/").join(" / ")}` : "Cloud Drive (root)";
    }

    function refreshTarget() {
      target.textContent = `☁️ ${label()}`;
      tree.querySelectorAll(".cd-row.cd-folder").forEach((row) => {
        row.classList.toggle("cd-current", row.dataset.path === (selected || ""));
      });
    }

    function makeRow(path, depth) {
      const kids = childrenOf(null, path).folderKids;
      const key = expKey(null, path);
      const open = drive.expanded.has(key);
      const row = document.createElement("div");
      row.className = "cd-row cd-folder" + (path === (selected || "") ? " cd-current" : "");
      row.dataset.path = path;
      row.style.paddingLeft = `${6 + depth * 16}px`;
      const tw = document.createElement("span");
      tw.className = "cd-tw";
      tw.textContent = kids.length ? (open ? "▾" : "▸") : "·";
      row.appendChild(tw);
      const icon = document.createElement("span");
      icon.className = "cd-icon";
      icon.textContent = path === "" ? "☁️" : "📁";
      row.appendChild(icon);
      const nm = document.createElement("span");
      nm.className = "cd-name";
      nm.textContent = path === "" ? "My Drive" : path.split("/").pop();
      row.appendChild(nm);
      row.title = path === "" ? "Cloud Drive root" : `Save to ${path}`;
      if (kids.length) {
        tw.addEventListener("click", (e) => {
          e.stopPropagation();
          if (drive.expanded.has(key)) drive.expanded.delete(key);
          else drive.expanded.add(key);
          renderTree();
        });
      }
      row.addEventListener("click", () => {
        selected = path === "" ? null : path;
        if (kids.length) {
          if (drive.expanded.has(key)) drive.expanded.delete(key);
          else drive.expanded.add(key);
        }
        renderTree();
      });
      return row;
    }

    function renderKids(path, depth, container) {
      const kids = childrenOf(null, path).folderKids;
      if (!kids.length) {
        const empty = document.createElement("div");
        empty.className = "cd-empty";
        empty.style.padding = "2px 12px";
        empty.textContent = "Empty.";
        container.appendChild(empty);
        return;
      }
      for (const d of kids) {
        container.appendChild(makeRow(d.path, depth));
        if (drive.expanded.has(expKey(null, d.path))) {
          const box = document.createElement("div");
          box.className = "cd-kids";
          renderKids(d.path, depth + 1, box);
          container.appendChild(box);
        }
      }
    }

    function renderTree() {
      tree.innerHTML = "";
      tree.appendChild(makeRow("", 0));
      if (drive.expanded.has(expKey(null, ""))) {
        const box = document.createElement("div");
        box.className = "cd-kids";
        renderKids("", 1, box);
        tree.appendChild(box);
      }
      refreshTarget();
    }

    function onEsc(e) {
      if (e.key === "Escape") finish(undefined);
    }
    cancelBtn.addEventListener("click", () => finish(undefined));
    okBtn.addEventListener("click", () =>
      finish({ folderPath: selected, prompt: promptEl ? promptEl.value.trim() || null : null })
    );
    close.addEventListener("click", () => finish(undefined));
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) finish(undefined); });
    document.addEventListener("keydown", onEsc);
    document.body.appendChild(overlay);
    renderTree();
    return promise;
  };

  // ── "Pick Cloud Drive files" picker (multi-select) ──
  // Used by the chat toolkit dialog ("Add cloud file"): a checkbox tree over My Drive +
  // every workspace. Resolves with an array of file objects { id, name, folder_path,
  // workspace_id, size } ([] when cancelled).
  // Toolkit generation config (per-file size cap + supported formats), fetched once from
  // /toolkit/config so the picker can mark files a generation job would refuse: oversized
  // files and file types with no text extractor.
  let toolkitCfg = null;
  async function toolkitLimits() {
    if (toolkitCfg === null) {
      try {
        const c = await apiFetch("/toolkit/config");
        toolkitCfg = {
          maxFileBytes: c.max_file_bytes || 0,
          extensions: new Set(c.supported_extensions || []),
        };
      } catch { toolkitCfg = { maxFileBytes: 0, extensions: new Set() }; }
    }
    return toolkitCfg;
  }
  window.pickCloudFiles = async ({ title = "Pick cloud files", okLabel = "Add" } = {}) => {
    if (!getToken()) { Viewer.toast("Sign in to pick cloud files."); return []; }
    try { await loadDrive(); } catch { /* keep whatever files we already have */ }
    const cfg = await toolkitLimits();
    const maxBytes = cfg.maxFileBytes;
    const extSet = cfg.extensions;

    const overlay = document.createElement("div");
    overlay.className = "overlay";
    const modal = document.createElement("div");
    modal.className = "modal cd-picker-modal";

    const header = document.createElement("div");
    header.className = "modal-header";
    const h = document.createElement("h3");
    h.textContent = title;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "modal-close";
    close.innerHTML = "&times;";
    close.title = "Close";
    header.append(h, close);

    const tree = document.createElement("div");
    tree.className = "cd-picker-tree";

    const footer = document.createElement("div");
    footer.className = "cd-pick-count";

    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "Cancel";
    const okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "primary";
    okBtn.textContent = okLabel;
    actions.append(cancelBtn, okBtn);

    modal.append(header, tree, footer, actions);
    overlay.appendChild(modal);

    const selected = new Map(); // file id -> file object

    function refreshFooter() {
      footer.textContent = selected.size
        ? `${selected.size} file${selected.size === 1 ? "" : "s"} selected`
        : "No files selected";
    }
    refreshFooter();

    let resolveVal;
    const promise = new Promise((res) => { resolveVal = res; });
    function finish(val) {
      document.removeEventListener("keydown", onEsc);
      overlay.remove();
      resolveVal(val);
    }

    function makeFileRow(f, scope, depth) {
      const row = document.createElement("div");
      row.className = "cd-row cd-file cd-pick-file";
      row.dataset.id = f.id;
      row.style.paddingLeft = `${6 + depth * 16}px`;
      const dot = (f.name || "").lastIndexOf(".");
      const ext = dot > 0 ? f.name.slice(dot).toLowerCase() : "";
      const badType = extSet.size > 0 && !extSet.has(ext);
      const tooBig = maxBytes > 0 && (f.size || 0) > maxBytes;
      const blocked = badType || tooBig;
      if (blocked) row.classList.add("cd-pick-over");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "cd-pick-check";
      box.checked = selected.has(f.id);
      box.disabled = blocked;
      const icon = document.createElement("span");
      icon.className = "cd-icon";
      icon.textContent = "📄";
      const nm = document.createElement("span");
      nm.className = "cd-name";
      nm.textContent = f.name;
      nm.title = f.folder_path ? `${f.folder_path}/${f.name}` : f.name;
      const toggle = () => {
        if (blocked) return;
        if (selected.has(f.id)) selected.delete(f.id);
        else selected.set(f.id, { id: f.id, name: f.name, folder_path: f.folder_path || "", workspace_id: scope, size: f.size || 0 });
        box.checked = selected.has(f.id);
        refreshFooter();
      };
      row.addEventListener("click", toggle);
      box.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
      row.append(box, icon, nm);
      if (badType) {
        const tag = document.createElement("span");
        tag.className = "cd-pick-over-tag";
        tag.textContent = "unsupported format";
        row.append(tag);
      } else if (tooBig) {
        const tag = document.createElement("span");
        tag.className = "cd-pick-over-tag";
        tag.textContent = `over limit (>${Math.round(maxBytes / (1024 * 1024))}MB)`;
        row.append(tag);
      }
      const size = document.createElement("span");
      size.className = "cd-pick-size";
      size.textContent = fmtSize(f.size || 0);
      row.append(size);
      return row;
    }

    function makeFolderRow(scope, path, depth) {
      const key = expKey(scope, path);
      const open = drive.expanded.has(key);
      const row = document.createElement("div");
      row.className = "cd-row cd-folder";
      row.style.paddingLeft = `${6 + depth * 16}px`;
      const tw = document.createElement("span");
      tw.className = "cd-tw";
      const hasKids = childrenOf(scope, path).folderKids.length > 0;
      tw.textContent = hasKids ? (open ? "▾" : "▸") : "·";
      const icon = document.createElement("span");
      icon.className = "cd-icon";
      icon.textContent = path === "" ? "☁️" : "📁";
      const nm = document.createElement("span");
      nm.className = "cd-name";
      nm.textContent = path === "" ? "My Drive" : path.split("/").pop();
      row.append(tw, icon, nm);
      const expand = () => {
        if (drive.expanded.has(key)) drive.expanded.delete(key);
        else drive.expanded.add(key);
        render();
      };
      tw.addEventListener("click", (e) => { e.stopPropagation(); expand(); });
      row.addEventListener("dblclick", (e) => { e.stopPropagation(); expand(); });
      return row;
    }

    function renderScopeChildren(scope, path, depth, container) {
      const kids = childrenOf(scope, path);
      for (const d of kids.folderKids) {
        container.appendChild(makeFolderRow(scope, d.path, depth));
        if (drive.expanded.has(expKey(scope, d.path))) {
          const box = document.createElement("div");
          box.className = "cd-kids";
          renderScopeChildren(scope, d.path, depth + 1, box);
          container.appendChild(box);
        }
      }
      for (const f of kids.fileKids) container.appendChild(makeFileRow(f, scope, depth));
    }

    function render() {
      tree.innerHTML = "";
      for (const scope of [null, ...drive.workspaces.map((w) => w.id)]) {
        const key = expKey(scope, "");
        const open = drive.expanded.has(key);
        const root = document.createElement("div");
        root.className = "cd-row cd-folder";
        const tw = document.createElement("span");
        tw.className = "cd-tw";
        const hasKids = childrenOf(scope, "").folderKids.length > 0;
        tw.textContent = hasKids ? (open ? "▾" : "▸") : "·";
        const icon = document.createElement("span");
        icon.className = "cd-icon";
        icon.textContent = scope == null ? "☁️" : "🗂";
        const nm = document.createElement("span");
        nm.className = "cd-name";
        nm.textContent = scope == null ? "My Drive" : wsName(scope);
        const expand = () => {
          if (drive.expanded.has(key)) drive.expanded.delete(key);
          else drive.expanded.add(key);
          render();
        };
        tw.addEventListener("click", (e) => { e.stopPropagation(); expand(); });
        root.addEventListener("dblclick", (e) => { e.stopPropagation(); expand(); });
        root.append(tw, icon, nm);
        tree.appendChild(root);
        if (open) {
          const box = document.createElement("div");
          box.className = "cd-kids";
          renderScopeChildren(scope, "", 1, box);
          tree.appendChild(box);
        }
      }
    }

    function onEsc(e) {
      if (e.key === "Escape") finish([]);
    }
    cancelBtn.addEventListener("click", () => finish([]));
    okBtn.addEventListener("click", () => finish([...selected.values()]));
    close.addEventListener("click", () => finish([]));
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) finish([]); });
    document.addEventListener("keydown", onEsc);
    document.body.appendChild(overlay);
    render();
    return promise;
  };

  // If a cloud drive is already visible (e.g. re-opened window), refresh it.
  if (!cloudEl.classList.contains("hidden")) loadDrive();
})();
