// Electron main process for the DeepDive learning workbench.
//
// Serves the desktop renderer (apps/desktop/renderer) over a privileged `app://`
// protocol, proxies /api to the FastAPI backend, and gives the renderer access to
// local files through a `local://` protocol + a small IPC surface (folder pick,
// file tree, open-with-OS-default, text read, screenshot save).
const { app, BrowserWindow, protocol, net, ipcMain, dialog, shell, Menu } = require("electron");
const path = require("path");
const fs = require("fs");
const os = require("os");
const { Readable } = require("stream");
const { pathToFileURL } = require("url");
const { PDFDocument, rgb } = require("pdf-lib");
const WordExtractor = require("word-extractor"); // legacy binary .doc text extraction

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return rgb(1, 0.84, 0.33);
  const n = parseInt(m[1], 16);
  return rgb(((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255);
}

// Compare two dotted numeric versions ("1.2.0"); true when a > b. Prerelease tags
// are ignored for the simple stable-check path.
function semverGt(a, b) {
  const pa = String(a).split(".").map(Number);
  const pb = String(b).split(".").map(Number);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] || 0) > (pb[i] || 0)) return true;
    if ((pa[i] || 0) < (pb[i] || 0)) return false;
  }
  return false;
}

// Tiny JSON preference store next to the app's userData dir (window bounds,
// remember-bounds flag, etc.). No database involved.
function prefsPath() {
  return path.join(app.getPath("userData"), "prefs.json");
}
function readPrefs() {
  try {
    return JSON.parse(fs.readFileSync(prefsPath(), "utf-8"));
  } catch {
    return {};
  }
}
function writePrefs(patch) {
  try {
    const all = { ...readPrefs(), ...patch };
    fs.writeFileSync(prefsPath(), JSON.stringify(all, null, 2));
  } catch { /* non-fatal */ }
}

const BACKEND = "http://localhost:8300";
const RENDERER_DIR = path.join(__dirname, "renderer");

const MAX_TREE_DEPTH = 8;
const IGNORED_DIRS = new Set(["node_modules", ".git", ".svn", "__pycache__", ".venv", "venv", "dist", "build"]);
const MAX_TEXT_PREVIEW = 2 * 1024 * 1024; // 2 MB

// `app://` and `local://` must be standard + secure so fetch and relative URLs
// resolve like a normal origin.
protocol.registerSchemesAsPrivileged([
  { scheme: "app", privileges: { standard: true, secure: true, supportFetchAPI: true } },
  { scheme: "local", privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } },
]);

async function proxy(target, request) {
  const init = { method: request.method, headers: request.headers };
  // net.fetch defaults to GET; forward the body for anything that has one.
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.headers = new Headers(request.headers);
    // A zero-length body must not be forwarded as a stream: Chromium aborts empty
    // streamed bodies with "Premature close", which broke creating empty text files
    // (their single upload chunk is 0 bytes). Send an explicit empty body instead.
    // Binary multipart / file-chunk payloads still stream byte-for-byte below.
    if (init.headers.get("content-length") === "0") {
      init.headers.delete("content-length");
      init.body = "";
    } else {
      init.headers.delete("content-length");
      init.body = request.body;
      init.duplex = "half"; // required by net.fetch for streamed (non-string) bodies
    }
  }
  return net
    .fetch(target, init)
    .catch((err) => new Response(`Backend unavailable: ${target} — ${err.message}`, { status: 502 }));
}

function handleAppRequest(request) {
  const { pathname, search } = new URL(request.url);

  // API: the renderer calls /api/... (same convention as the web app); strip prefix.
  if (pathname.startsWith("/api/")) {
    return proxy(BACKEND + pathname.slice("/api".length) + search, request);
  }
  // Cached TTS audio / images are served by the backend at /audio and /images.
  if (pathname.startsWith("/audio/") || pathname.startsWith("/images/")) {
    return proxy(BACKEND + pathname + search, request);
  }

  // Static files from the desktop renderer, falling back to index.html (SPA).
  const relative = pathname === "/" ? "index.html" : pathname.replace(/^\//, "");
  let filePath = path.join(RENDERER_DIR, relative);
  if (!fs.existsSync(filePath)) {
    filePath = path.join(RENDERER_DIR, "index.html");
  }
  return net.fetch(pathToFileURL(filePath).toString());
}

const MIME = {
  mp4: "video/mp4", webm: "video/webm", mov: "video/quicktime", m4v: "video/x-m4v",
  mp3: "audio/mpeg", wav: "audio/wav", m4a: "audio/mp4", flac: "audio/flac", ogg: "audio/ogg",
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
  webp: "image/webp", bmp: "image/bmp", svg: "image/svg+xml",
  pdf: "application/pdf",
};

function mimeOf(filePath) {
  const ext = path.extname(filePath).slice(1).toLowerCase();
  return MIME[ext] || "application/octet-stream";
}

// Decode a text buffer, sniffing the encoding: UTF-8 (with/without BOM), falling
// back to GB18030 (a superset of GBK/GB2312) for legacy Chinese subtitle/text files.
function decodeText(buf) {
  if (buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) {
    return buf.subarray(3).toString("utf-8");
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buf);
  } catch {
    return new TextDecoder("gb18030").decode(buf);
  }
}

// `local://file/?path=<abs-path>` → stream the local file. The path travels in the
// query string so Windows drive letters never mangle the URL authority. Byte-range
// requests are honoured so <video>/<audio> seeking and PDF.js random access work; a
// permissive CORS header lets the renderer canvas-read frames and lets PDF.js fetch
// the document, even though `local://` and `app://` are different origins.
function handleLocalRequest(request) {
  const filePath = new URL(request.url).searchParams.get("path");
  if (!filePath) return new Response("missing path", { status: 400 });

  let stat;
  try {
    stat = fs.statSync(filePath);
  } catch {
    return new Response("not found", { status: 404 });
  }
  const size = stat.size;

  const headers = new Headers();
  headers.set("Accept-Ranges", "bytes");
  headers.set("Content-Type", mimeOf(filePath));
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length");

  const range = request.headers.get("range");
  if (range) {
    const m = /bytes=(\d*)-(\d*)/.exec(range);
    if (m) {
      let start = m[1] === "" ? null : parseInt(m[1], 10);
      let end = m[2] === "" ? null : parseInt(m[2], 10);
      if (start === null) {
        // Suffix range: "bytes=-N" → the last N bytes.
        const suffix = parseInt(m[2], 10);
        start = Math.max(0, size - suffix);
        end = size - 1;
      } else if (end === null || end >= size) {
        end = size - 1;
      }
      if (start > end || start >= size) {
        headers.set("Content-Range", `bytes */${size}`);
        return new Response(null, { status: 416, headers });
      }
      headers.set("Content-Range", `bytes ${start}-${end}/${size}`);
      headers.set("Content-Length", String(end - start + 1));
      const webStream = Readable.toWeb(fs.createReadStream(filePath, { start, end }));
      return new Response(webStream, { status: 206, headers });
    }
  }

  headers.set("Content-Length", String(size));
  const webStream = Readable.toWeb(fs.createReadStream(filePath));
  return new Response(webStream, { status: 200, headers });
}

function readTree(dir, depth = 0) {
  if (depth > MAX_TREE_DEPTH) return [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }
  const result = [];
  for (const entry of entries) {
    if (entry.name.startsWith(".")) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (IGNORED_DIRS.has(entry.name)) continue;
      result.push({ name: entry.name, path: full, type: "dir", children: readTree(full, depth + 1) });
    } else if (entry.isFile()) {
      result.push({ name: entry.name, path: full, type: "file" });
    }
  }
  result.sort((a, b) =>
    a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1
  );
  return result;
}

function registerIpcHandlers() {
  ipcMain.handle("pick-folder", async () => {
    const { canceled, filePaths } = await dialog.showOpenDialog({ properties: ["openDirectory"] });
    return canceled || filePaths.length === 0 ? null : filePaths[0];
  });

  ipcMain.handle("pick-file", async () => {
    const { canceled, filePaths } = await dialog.showOpenDialog({ properties: ["openFile"] });
    return canceled || filePaths.length === 0 ? null : filePaths[0];
  });

  // Copy a file into a workspace folder, resolving name collisions with "(n)".
  ipcMain.handle("copy-into-workspace", (_event, { src, destDir }) => {
    if (!src || !destDir) return { ok: false, error: "No file or workspace folder selected." };
    try {
      if (!fs.existsSync(destDir) || !fs.statSync(destDir).isDirectory()) {
        return { ok: false, error: "The workspace folder no longer exists; reopen it." };
      }
      let name = path.basename(src);
      let target = path.join(destDir, name);
      const ext = path.extname(name);
      const stem = path.basename(name, ext);
      let n = 1;
      while (fs.existsSync(target)) {
        target = path.join(destDir, `${stem}(${n})${ext}`);
        n += 1;
      }
      fs.copyFileSync(src, target);
      return { ok: true, name: path.basename(target), path: target };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // Delete a file inside the open workspace. Files only (never directories), and only
  // paths that resolve inside the workspace folder — a stray path is refused.
  ipcMain.handle("delete-file", (_event, { filePath, workspaceDir }) => {
    if (!filePath || !workspaceDir) return { ok: false, error: "No file or workspace folder selected." };
    try {
      const root = path.resolve(workspaceDir);
      const target = path.resolve(filePath);
      if (target !== root && !target.startsWith(root + path.sep)) {
        return { ok: false, error: "Refusing to delete a path outside the workspace." };
      }
      if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
        return { ok: false, error: "Not a file (or it no longer exists)." };
      }
      fs.unlinkSync(target);
      return { ok: true, name: path.basename(target) };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // Delete a folder inside the open workspace, recursively. Workspace-bounded like
  // delete-file, and the workspace root itself is refused so the user can't wipe it.
  ipcMain.handle("delete-folder", (_event, { dirPath, workspaceDir }) => {
    if (!dirPath || !workspaceDir) return { ok: false, error: "No folder or workspace folder selected." };
    try {
      const root = path.resolve(workspaceDir);
      const target = path.resolve(dirPath);
      if (target !== root && !target.startsWith(root + path.sep)) {
        return { ok: false, error: "Refusing to delete a path outside the workspace." };
      }
      if (target === root) {
        return { ok: false, error: "Refusing to delete the workspace root." };
      }
      if (!fs.existsSync(target) || !fs.statSync(target).isDirectory()) {
        return { ok: false, error: "Not a folder (or it no longer exists)." };
      }
      fs.rmSync(target, { recursive: true, force: true });
      return { ok: true, name: path.basename(target) };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // Download a cloud file to a per-asset temp cache so the path-based in-window
  // viewer can render it (PDF/image/video/audio all stream via the `local://`
  // protocol). The cache path is keyed by asset id: stable across opens (annotation
  // sidecars survive), and the filename extension is whitelisted so a hostile name
  // can't escape the cache directory.
  ipcMain.handle("cloud-cache", async (_event, { assetId, name, token }) => {
    if (!assetId || !token) return { ok: false, error: "No asset or token." };
    const ext = path.extname(String(name || "")).slice(1).toLowerCase();
    const safeExt = /^[a-z0-9]{1,10}$/i.test(ext) ? ext : "bin";
    const safeId = String(assetId).replace(/[^\w-]/g, "");
    const dir = path.join(app.getPath("temp"), "deepdive-cloud");
    const target = path.join(dir, `${safeId}.${safeExt}`);
    try {
      fs.mkdirSync(dir, { recursive: true });
      const res = await net.fetch(`${BACKEND}/files/${safeId}/download`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try { const b = await res.json(); if (b && b.detail) detail = b.detail; } catch { /* not JSON */ }
        return { ok: false, error: detail };
      }
      const out = fs.createWriteStream(target);
      await new Promise((resolve, reject) => {
        Readable.fromWeb(res.body)
          .on("error", reject)
          .pipe(out)
          .on("error", reject)
          .on("finish", resolve);
      });
      return { ok: true, path: target };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // Create a folder inside the workspace (or a subfolder of it). Like delete-file,
  // only paths that resolve inside the workspace folder are allowed.
  ipcMain.handle("create-folder", (_event, { workspaceDir, parentDir, name }) => {
    if (!workspaceDir || !parentDir || !name) return { ok: false, error: "No file or workspace folder selected." };
    const clean = String(name).trim();
    if (!clean || /[\\/]/.test(clean)) return { ok: false, error: "Folder name must be a single non-empty name." };
    try {
      const root = path.resolve(workspaceDir);
      const parent = path.resolve(parentDir);
      if (parent !== root && !parent.startsWith(root + path.sep)) {
        return { ok: false, error: "Refusing to create a folder outside the workspace." };
      }
      const target = path.join(parent, clean);
      if (fs.existsSync(target)) return { ok: false, error: `"${clean}" already exists.` };
      fs.mkdirSync(target);
      return { ok: true, name: clean, path: target };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // Create a text file inside the workspace, resolving name collisions with "(n)".
  ipcMain.handle("create-text-file", (_event, { workspaceDir, parentDir, name, content }) => {
    if (!workspaceDir || !parentDir || !name) return { ok: false, error: "No file or workspace folder selected." };
    const clean = String(name).trim();
    if (!clean || /[\\/]/.test(clean)) return { ok: false, error: "File name must be a single non-empty name." };
    try {
      const root = path.resolve(workspaceDir);
      const parent = path.resolve(parentDir);
      if (parent !== root && !parent.startsWith(root + path.sep)) {
        return { ok: false, error: "Refusing to create a file outside the workspace." };
      }
      const ext = path.extname(clean);
      const stem = path.basename(clean, ext);
      let target = path.join(parent, clean);
      let n = 1;
      while (fs.existsSync(target)) {
        target = path.join(parent, `${stem}(${n})${ext}`);
        n += 1;
      }
      fs.writeFileSync(target, String(content ?? ""), "utf8");
      return { ok: true, name: path.basename(target), path: target };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // Move a file or folder into another folder of the workspace (drag-and-drop).
  // Both paths must resolve inside the workspace; a directory cannot be moved into
  // itself or a descendant; a name collision at the destination is refused.
  ipcMain.handle("move-path", (_event, { workspaceDir, srcPath, destDir }) => {
    if (!workspaceDir || !srcPath || !destDir) return { ok: false, error: "No file or workspace folder selected." };
    try {
      const root = path.resolve(workspaceDir);
      const src = path.resolve(srcPath);
      const dest = path.resolve(destDir);
      const inside = (p) => p === root || p.startsWith(root + path.sep);
      if (!inside(src) || !inside(dest)) {
        return { ok: false, error: "Refusing to move a path outside the workspace." };
      }
      if (!fs.existsSync(src)) return { ok: false, error: "Source no longer exists." };
      if (!fs.statSync(dest).isDirectory()) return { ok: false, error: "Destination is not a folder." };
      if (src === dest) return { ok: false, error: "Already there." };
      if (dest.startsWith(src + path.sep)) {
        return { ok: false, error: "Cannot move a folder into itself or a descendant." };
      }
      const name = path.basename(src);
      const target = path.join(dest, name);
      if (fs.existsSync(target)) return { ok: false, error: `"${name}" already exists there.` };
      fs.renameSync(src, target);
      return { ok: true, name, path: target };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  ipcMain.handle("read-tree", (_event, dir) => readTree(dir));

  ipcMain.handle("open-external", (_event, filePath) => shell.openPath(filePath));

  ipcMain.handle("open-url", (_event, url) => shell.openExternal(url));

  ipcMain.handle("app-version", () => app.getVersion());

  ipcMain.handle("set-pref", (_event, key, value) => {
    writePrefs({ [key]: value });
  });

  // Reflect the open workspace's folder name in the File menu label ("" resets it).
  ipcMain.on("update-workspace-label", (_event, name) => {
    if (openWorkspaceMenuItem) {
      openWorkspaceMenuItem.label = name ? `Open Workspace: ${name}` : "Open Workspace…";
    }
  });

  // Check for a newer release on GitHub by comparing the latest tag to the
  // packaged version (SemVer 2.0). The release page is opened by the renderer.
  ipcMain.handle("check-update", async () => {
    try {
      const res = await net.fetch(
        "https://api.github.com/repos/Eric-LLMs/DeepDive/releases/latest",
        { headers: { Accept: "application/vnd.github+json", "User-Agent": "DeepDive-Desktop" } }
      );
      // No releases published yet → nothing newer than the current build.
      if (res.status === 404) {
        return { ok: true, status: "latest", latest: "", current: app.getVersion(), url: "" };
      }
      if (!res.ok) return { ok: false, error: `Update server responded ${res.status}` };
      const rel = await res.json();
      const latest = String(rel.tag_name || "").replace(/^v/, "");
      const current = app.getVersion();
      return {
        ok: true,
        status: semverGt(latest, current) ? "update" : "latest",
        latest,
        current,
        name: rel.name,
        notes: rel.body,
        url: rel.html_url,
      };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // Read a file as raw bytes (Office documents are ZIPs, so the renderer needs the
  // binary content, not UTF-8 text). The Buffer is delivered to the renderer as a
  // Uint8Array via structured clone.
  ipcMain.handle("read-file-bytes", (_event, filePath) => {
    try {
      return { ok: true, data: fs.readFileSync(filePath) };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // Pull embedded raster images (PNG/JPEG/GIF/BMP) out of a .doc's raw bytes so the
  // preview can show them. Best-effort: images are extracted in file order, deduped by
  // content, and rendered without Word's exact layout. WMF/EMF vectors and spurious byte
  // patterns are skipped (weak signatures like BMP are size-validated).
  function extractEmbeddedImages(buf) {
    const crypto = require("crypto");
    const out = [];
    const seen = new Set();
    const push = (start, end, mime) => {
      if (end <= start || end - start < 16) return;
      const blob = buf.subarray(start, end);
      const h = crypto.createHash("sha1").update(blob).digest("hex");
      if (seen.has(h)) return;
      seen.add(h);
      out.push({ mime, b64: blob.toString("base64") });
    };
    const IEND = Buffer.from([0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82]);
    const sigs = {
      png: [0x89, 0x50, 0x4e, 0x47],
      jpg: [0xff, 0xd8, 0xff],
      gif: [0x47, 0x49, 0x46, 0x38],
      bmp: [0x42, 0x4d],
    };
    const matches = (i, s) => { for (let j = 0; j < s.length; j++) if (buf[i + j] !== s[j]) return false; return true; };
    let i = 0;
    while (i < buf.length - 8) {
      if (matches(i, sigs.png)) {
        const ie = buf.indexOf(IEND, i + 8);
        const end = ie > 0 ? ie + 8 : i + 8;
        push(i, end, "image/png");
        i = end;
      } else if (matches(i, sigs.jpg)) {
        let e = i + 4;
        while (e < buf.length - 1 && !(buf[e] === 0xff && buf[e + 1] === 0xd9)) e++;
        const end = e < buf.length - 1 ? e + 2 : i + 4;
        push(i, end, "image/jpeg");
        i = end;
      } else if (matches(i, sigs.gif)) {
        let e = i + 10;
        while (e < buf.length && buf[e] !== 0x3b) e++;
        const end = e < buf.length ? e + 1 : i + 10;
        push(i, end, "image/gif");
        i = end;
      } else if (matches(i, sigs.bmp)) {
        const size = buf.readUInt32LE(i + 2);
        if (size >= 54 && i + size <= buf.length) push(i, i + size, "image/bmp");
        i += 4;
      } else {
        i += 1;
      }
    }
    return out;
  }

  // Extract the text of a legacy binary Word (.doc) document with word-extractor (pure
  // JS, runs in the main process), plus any embedded raster images so the preview isn't
  // blind for image-heavy documents. Layout/positioning aren't preserved. Any parse
  // failure falls back to the system default app in the renderer.
  ipcMain.handle("word-extract", async (_event, filePath) => {
    try {
      const extractor = new WordExtractor();
      const doc = await extractor.extract(filePath);
      const text = doc.getBody();
      const images = extractEmbeddedImages(fs.readFileSync(filePath));
      if ((!text || !text.trim()) && images.length === 0) {
        return { ok: false, error: "The document contains no extractable text or images." };
      }
      return { ok: true, content: text || "", images };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  ipcMain.handle("find-subtitle", (_event, videoPath) => {
    const dir = path.dirname(videoPath);
    const base = path.basename(videoPath, path.extname(videoPath));
    for (const ext of [".srt", ".vtt", ".lrc"]) {
      const candidate = path.join(dir, base + ext);
      if (fs.existsSync(candidate)) return candidate;
    }
    return null;
  });

  ipcMain.handle("pick-subtitle", async (_event, startDir) => {
    const { canceled, filePaths } = await dialog.showOpenDialog({
      // Open the dialog in the video's folder by default so a matching subtitle is
      // right next to the player.
      defaultPath: startDir || undefined,
      properties: ["openFile"],
      filters: [
        { name: "Subtitle files", extensions: ["srt", "vtt", "lrc"] },
        { name: "All files", extensions: ["*"] },
      ],
    });
    return canceled || filePaths.length === 0 ? null : filePaths[0];
  });

  ipcMain.handle("read-text", (_event, filePath) => {
    try {
      const stat = fs.statSync(filePath);
      if (stat.size > MAX_TEXT_PREVIEW) {
        return { ok: false, error: "File too large to preview." };
      }
      const buf = fs.readFileSync(filePath);
      if (buf.includes(0)) {
        return { ok: false, error: "Binary file — cannot preview as text." };
      }
      return { ok: true, content: decodeText(buf) };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  ipcMain.handle("read-annotations", (_event, pdfPath) => {
    const annotPath = pdfPath + ".annot.json";
    try {
      if (!fs.existsSync(annotPath)) return { ok: true, data: null };
      return { ok: true, data: JSON.parse(fs.readFileSync(annotPath, "utf-8")) };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  ipcMain.handle("save-annotations", (_event, { pdfPath, data }) => {
    try {
      fs.writeFileSync(pdfPath + ".annot.json", JSON.stringify(data, null, 2), "utf-8");
      return { ok: true };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // Flatten sidecar annotations into a copy of the PDF so other readers can see them.
  ipcMain.handle("embed-annotations", async (_event, { pdfPath, annotations }) => {
    try {
      const doc = await PDFDocument.load(fs.readFileSync(pdfPath));
      const pages = doc.getPages();
      for (const s of annotations.strokes || []) {
        const page = pages[s.page];
        if (!page || !s.points || !s.points.length) continue;
        const { width, height } = page.getSize();
        const px = (p) => ({ x: p.x * width, y: height - p.y * height });
        const color = hexToRgb(s.color);
        const borderWidth = Math.max(0.5, (s.width || 3) * 0.75);
        if (s.points.length === 1) {
          const { x, y } = px(s.points[0]);
          page.drawCircle({ x, y, size: borderWidth, color, borderColor: color, borderWidth: 0 });
          continue;
        }
        const d = "M " + s.points.map((p) => {
          const q = px(p);
          return `${q.x.toFixed(2)} ${q.y.toFixed(2)}`;
        }).join(" L ");
        page.drawSvgPath(d, { borderColor: color, borderWidth });
      }
      for (const n of annotations.notes || []) {
        const page = pages[n.page];
        if (!page) continue;
        const { width, height } = page.getSize();
        const x = n.x * width;
        const y = height - n.y * height;
        page.drawRectangle({
          x: x - 4, y: y - 4, width: 8, height: 8,
          color: rgb(1, 0.84, 0.33),
          borderColor: rgb(0.72, 0.55, 0.1),
          borderWidth: 0.6,
        });
      }
      const outPath = pdfPath.replace(/\.pdf$/i, "") + ".annotated.pdf";
      fs.writeFileSync(outPath, await doc.save());
      return { ok: true, path: outPath };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  ipcMain.handle("save-screenshot", async (_event, { dataURL, defaultName }) => {
    const { canceled, filePath } = await dialog.showSaveDialog({
      defaultPath: defaultName || "screenshot.png",
      filters: [{ name: "PNG Image", extensions: ["png"] }],
    });
    if (canceled || !filePath) return null;
    const base64 = dataURL.replace(/^data:image\/png;base64,/, "");
    fs.writeFileSync(filePath, Buffer.from(base64, "base64"));
    return filePath;
  });

  // Pick an avatar image: open the OS file picker, read the bytes, and return a
  // base64 string + mime so the renderer can POST it as multipart to /auth/me/avatar.
  ipcMain.handle("pick-image", async () => {
    const { canceled, filePaths } = await dialog.showOpenDialog({
      properties: ["openFile"],
      filters: [
        { name: "Images", extensions: ["png", "jpg", "jpeg", "webp", "gif"] },
        { name: "All files", extensions: ["*"] },
      ],
    });
    if (canceled || filePaths.length === 0) return null;
    const filePath = filePaths[0];
    try {
      const buf = fs.readFileSync(filePath);
      if (buf.length > 2 * 1024 * 1024) return { ok: false, error: "头像图片不能超过 2MB" };
      const ext = path.extname(filePath).slice(1).toLowerCase();
      const mime =
        { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp", gif: "image/gif" }[ext] ||
        "application/octet-stream";
      return { ok: true, name: path.basename(filePath), mime, base64: buf.toString("base64") };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });
}

let openWorkspaceMenuItem = null;

function setupMenu() {
  const menu = Menu.buildFromTemplate([
    {
      label: "File",
      submenu: [
        {
          id: "open-workspace",
          label: "Open Workspace…",
          click: () => {
            const win = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
            if (win) win.webContents.send("menu-open-workspace");
          },
        },
        {
          label: "Add File to Workspace",
          click: () => {
            const win = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
            if (win) win.webContents.send("menu-add-file");
          },
        },
      ],
    },
    {
      label: "Edit",
      submenu: [
        { role: "undo" }, { role: "redo" }, { type: "separator" },
        { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "reload" }, { type: "separator" },
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" }, { type: "separator" },
        {
          label: "Font Size…",
          click: () => {
            const win = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
            if (win) win.webContents.send("menu-settings", "window");
          },
        },
        { type: "separator" },
        { role: "togglefullscreen" },
        { role: "toggleDevTools" },
      ],
    },
    {
      label: "Help",
      submenu: [
        {
          label: "Help & Feedback",
          click: () => {
            const win = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
            if (win) win.webContents.send("menu-settings", "help");
          },
        },
        {
          label: "About",
          click: () => {
            const win = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
            if (win) win.webContents.send("menu-settings", "about");
          },
        },
        { type: "separator" },
        {
          label: "DeepDive on GitHub",
          click: () => shell.openExternal("https://github.com/Eric-LLMs/DeepDive"),
        },
      ],
    },
  ]);
  Menu.setApplicationMenu(menu);
  openWorkspaceMenuItem = menu.getMenuItemById("open-workspace");
}

function createWindow() {
  const saved = readPrefs().window || {};
  const remember = saved.rememberBounds !== false;
  const win = new BrowserWindow({
    width: remember && saved.width ? saved.width : 1280,
    height: remember && saved.height ? saved.height : 820,
    x: remember && Number.isInteger(saved.x) ? saved.x : undefined,
    y: remember && Number.isInteger(saved.y) ? saved.y : undefined,
    icon: path.join(__dirname, "deepdive.ico"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  // Surface renderer console output / load failures in the main-process log, and
  // mirror the [imported] diagnostics to a file the dev can inspect after a run.
  win.webContents.on("console-message", (_e, level, message, line, sourceId) => {
    console.log(`[renderer:${level}] ${message} (${sourceId}:${line})`);
    if (String(message).includes("[imported]")) {
      try {
        fs.appendFileSync(
          path.join(__dirname, "..", "..", "data", "desktop_console.log"),
          `${new Date().toISOString()} [renderer:${level}] ${message}\n`
        );
      } catch { /* non-fatal */ }
    }
  });
  win.webContents.on("did-fail-load", (_e, code, desc, url) => {
    console.error(`[renderer] did-fail-load ${code} ${desc} ${url}`);
  });
  win.loadURL("app://bundle/index.html");

  // Remember the last window position/size (unless the user turned it off).
  win.on("close", () => {
    if (readPrefs().window?.rememberBounds === false) return;
    if (win.isMaximized() || win.isMinimized()) return;
    const b = win.getBounds();
    writePrefs({ window: { ...readPrefs().window, x: b.x, y: b.y, width: b.width, height: b.height } });
  });
}

app.whenReady().then(() => {
  if (!fs.existsSync(path.join(RENDERER_DIR, "index.html"))) {
    console.error(`Renderer not found at ${RENDERER_DIR}.`);
  }
  // Windows taskbar grouping + icon: bind the app to its own AppUserModelID so
  // the taskbar shows deepdive.ico instead of the generic Electron icon.
  app.setAppUserModelId("com.deepdive.desktop");
  protocol.handle("app", handleAppRequest);
  protocol.handle("local", handleLocalRequest);
  registerIpcHandlers();
  setupMenu();
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
