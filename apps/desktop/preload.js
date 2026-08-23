// Preload: expose a minimal, safe desktop API to the renderer via contextBridge.
// The renderer can browse local folders and open files, but never gets raw Node
// access (contextIsolation stays on, nodeIntegration off).
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktopAPI", {
  // Open a directory picker; resolves to an absolute path or null.
  pickFolder: () => ipcRenderer.invoke("pick-folder"),

  // Recursively list a directory into a file tree (bounded depth, filtered).
  readTree: (dir) => ipcRenderer.invoke("read-tree", dir),

  // Pick a single file (for the File menu's "Add File to Workspace").
  pickFile: () => ipcRenderer.invoke("pick-file"),

  // Copy a file into the workspace folder (collision-safe): { ok, name, path } | { ok, error }.
  copyIntoWorkspace: (src, destDir) => ipcRenderer.invoke("copy-into-workspace", { src, destDir }),

  // Delete a file inside the open workspace: { ok, name } | { ok, error }.
  deleteFile: (filePath, workspaceDir) =>
    ipcRenderer.invoke("delete-file", { filePath, workspaceDir }),

  // Delete a folder (recursively) inside the open workspace: { ok, name } | { ok, error }.
  deleteFolder: (dirPath, workspaceDir) =>
    ipcRenderer.invoke("delete-folder", { dirPath, workspaceDir }),

  // Download a cloud file to a temp cache so the in-window viewer can render it.
  // { assetId, name, token } → { ok, path } | { ok, error }.
  cloudCache: (assetId, name, token) =>
    ipcRenderer.invoke("cloud-cache", { assetId, name, token }),

  // Create a folder inside the workspace (or a subfolder): { workspaceDir, parentDir, name }.
  createFolder: (o) => ipcRenderer.invoke("create-folder", o),

  // Create a text file inside the workspace (collision-safe): { workspaceDir, parentDir, name, content }.
  createTextFile: (o) => ipcRenderer.invoke("create-text-file", o),

  // Move a file/folder into another folder (drag-and-drop): { workspaceDir, srcPath, destDir }.
  movePath: (o) => ipcRenderer.invoke("move-path", o),

  // File menu → renderer: "Add File to Workspace" was clicked.
  onAddFileToWorkspace: (cb) => { ipcRenderer.on("menu-add-file", () => cb()); },

  // Help menu → renderer: open a specific settings tab ("help" | "about").
  onOpenSettings: (cb) => { ipcRenderer.on("menu-settings", (_e, tab) => cb(tab)); },

  // File menu → renderer: "Open Workspace" was clicked.
  onOpenWorkspace: (cb) => { ipcRenderer.on("menu-open-workspace", () => cb()); },

  // Tell the main process the open workspace's folder name (shown in the File menu).
  setWorkspaceLabel: (name) => ipcRenderer.send("update-workspace-label", name),

  // Open a path with the OS default application.
  openExternal: (path) => ipcRenderer.invoke("open-external", path),

  // Open a URL in the system default browser.
  openUrl: (url) => ipcRenderer.invoke("open-url", url),

  // Resolve the packaged app version (for About / Software Update).
  getAppVersion: () => ipcRenderer.invoke("app-version"),

  // Persist a preference into the main process store (e.g. window.rememberBounds).
  setPref: (key, value) => ipcRenderer.invoke("set-pref", key, value),

  // Compare against the latest GitHub release; { ok, status, latest, current, notes, url }.
  checkUpdate: () => ipcRenderer.invoke("check-update"),

  // Read a small text file as a UTF-8 string (for text/code preview).
  readText: (path) => ipcRenderer.invoke("read-text", path),

  // Convert a PowerPoint file to PDF (LibreOffice headless); { ok, pdfPath } | { ok, error }.
  convertSlides: (path) => ipcRenderer.invoke("convert-slides", path),

  // Find a sibling subtitle file (.srt/.vtt/.lrc) for a video, or null.
  findSubtitle: (videoPath) => ipcRenderer.invoke("find-subtitle", videoPath),

  // Open a file picker to choose a subtitle file manually; resolves to a path or null.
  // startDir (optional) opens the dialog in that folder by default.
  pickSubtitle: (startDir) => ipcRenderer.invoke("pick-subtitle", startDir),

  // Save a PNG screenshot (data URL) to a user-chosen location.
  saveScreenshot: (dataURL, defaultName) =>
    ipcRenderer.invoke("save-screenshot", { dataURL, defaultName }),

  // Pick an avatar image; resolves to { ok, name, mime, base64 } | { ok, error } | null.
  pickImage: () => ipcRenderer.invoke("pick-image"),

  // PDF annotations: read/write a sidecar <pdf>.annot.json next to the PDF.
  readAnnotations: (pdfPath) => ipcRenderer.invoke("read-annotations", pdfPath),
  saveAnnotations: (pdfPath, data) => ipcRenderer.invoke("save-annotations", { pdfPath, data }),

  // Flatten sidecar annotations into a copy of the PDF (other readers can see them).
  embedAnnotations: (pdfPath, annotations) => ipcRenderer.invoke("embed-annotations", { pdfPath, annotations }),
});
