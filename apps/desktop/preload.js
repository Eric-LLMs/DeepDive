// Preload: expose a minimal, safe desktop API to the renderer via contextBridge.
// The renderer can browse local folders and open files, but never gets raw Node
// access (contextIsolation stays on, nodeIntegration off).
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktopAPI", {
  // Open a directory picker; resolves to an absolute path or null.
  pickFolder: () => ipcRenderer.invoke("pick-folder"),

  // Recursively list a directory into a file tree (bounded depth, filtered).
  readTree: (dir) => ipcRenderer.invoke("read-tree", dir),

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
  pickSubtitle: () => ipcRenderer.invoke("pick-subtitle"),

  // Save a PNG screenshot (data URL) to a user-chosen location.
  saveScreenshot: (dataURL, defaultName) =>
    ipcRenderer.invoke("save-screenshot", { dataURL, defaultName }),

  // PDF annotations: read/write a sidecar <pdf>.annot.json next to the PDF.
  readAnnotations: (pdfPath) => ipcRenderer.invoke("read-annotations", pdfPath),
  saveAnnotations: (pdfPath, data) => ipcRenderer.invoke("save-annotations", { pdfPath, data }),

  // Flatten sidecar annotations into a copy of the PDF (other readers can see them).
  embedAnnotations: (pdfPath, annotations) => ipcRenderer.invoke("embed-annotations", { pdfPath, annotations }),
});
