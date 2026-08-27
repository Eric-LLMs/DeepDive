// Viewer: dispatch a file to the right in-window renderer by extension, falling back
// to the OS default app for formats the window can't render natively.
const Viewer = (() => {
  const state = { path: null, name: null, kind: null, openPath: null, zoom: 1 };

  // app.js registers this so every file toolbar can offer "attach to chat". The handler
  // receives "file" (attach the currently-open file) or "screenshot" (capture the window).
  let attachHandler = null;
  function setAttachHandler(fn) { attachHandler = fn; }

  const VIDEO_EXT = new Set(["mp4", "webm", "mov", "m4v"]);
  const AUDIO_EXT = new Set(["mp3", "wav", "m4a", "flac", "ogg"]);
  const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"]);
  const TEXT_EXT = new Set([
    "txt", "md", "json", "log", "yaml", "yml", "toml", "ini", "conf",
    "py", "js", "ts", "jsx", "tsx", "html", "css", "sh", "sql", "java", "c",
    "cpp", "h", "rs", "go", "xml",
  ]);
  // Office formats previewed in-window with pure-JS renderers (vendored bundles).
  // The PowerPoint family (.pptx/.ppsx/.potx/…) is all the same OOXML zip, so one
  // renderer covers them. Legacy binary .doc is extracted to plain text in the main
  // process (word-extractor); .ppt has no reliable JS parser and falls back to the
  // system default app via kindFor → "unknown".
  const DOCX_EXT = new Set(["docx"]);
  const DOC_EXT = new Set(["doc"]);
  const SHEET_EXT = new Set(["xlsx", "xls", "csv", "tsv"]);
  const PPTX_EXT = new Set(["pptx", "ppsx", "potx", "pptm", "ppsm", "potm"]);

  const MIN_ZOOM = 0.25;
  const MAX_ZOOM = 5;
  const ZOOM_STEP = 1.25;

  // Subtitle presentation settings (adjusted from the video toolbar). Persisted to
  // localStorage so the last style is restored on the next launch.
  const subStyle = {
    enabled: true,
    fontSize: 15,
    color: "#ffffff",
    bg: true,
    position: "bottom", // "top" | "middle" | "bottom"
  };
  const SUBSTYLE_KEY = "deepdive_subtitle_style";
  function saveSubStyle() {
    try { localStorage.setItem(SUBSTYLE_KEY, JSON.stringify(subStyle)); } catch { /* ignore */ }
  }
  function loadSubStyle() {
    try {
      const raw = localStorage.getItem(SUBSTYLE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (!saved || typeof saved !== "object") return;
      if (typeof saved.enabled === "boolean") subStyle.enabled = saved.enabled;
      if (typeof saved.fontSize === "number") subStyle.fontSize = Math.min(40, Math.max(10, saved.fontSize));
      if (typeof saved.color === "string") subStyle.color = saved.color;
      if (typeof saved.bg === "boolean") subStyle.bg = saved.bg;
      if (["top", "middle", "bottom"].includes(saved.position)) subStyle.position = saved.position;
    } catch { /* ignore */ }
  }
  loadSubStyle();

  // PDF annotation state (persisted to a sidecar <pdf>.annot.json next to the PDF).
  const annot = { tool: "none", color: "#ffd54a", width: 3 }; // "none" | "draw" | "erase" | "note"
  let annotations = { strokes: [], notes: [] };
  let annotPath = null;
  let liveStroke = null; // the stroke currently being drawn (not yet committed)

  function extOf(name) {
    // A Windows copy may leave a collision suffix after the extension, e.g.
    // "notes.docx (1)" — strip it so the real extension still matches.
    const clean = String(name).replace(/\s*\(\d+\)\s*$/, "");
    const i = clean.lastIndexOf(".");
    return i < 0 ? "" : clean.slice(i + 1).toLowerCase();
  }

  function baseName(name) {
    const i = name.lastIndexOf(".");
    return i < 0 ? name : name.slice(0, i);
  }

  // Parent directory of an absolute path (handles both "/" and "\" separators).
  function dirOf(pathStr) {
    const parts = pathStr.replace(/\\/g, "/").split("/");
    parts.pop();
    return parts.join("/");
  }

  // local://file/?path=<abs> — the renderer can't touch the raw filesystem, so media
  // and documents are streamed through the main process's `local` protocol.
  function localUrl(filePath) {
    return `local://file/?path=${encodeURIComponent(filePath)}`;
  }

  // ── Subtitle parsing (.srt / .vtt / .lrc → [{ start, end, text }]) ──
  function parseTimestamp(str) {
    // Accepts "HH:MM:SS,mmm", "HH:MM:SS.mmm", "MM:SS.mmm".
    const parts = str.trim().split(":").map((p) => parseFloat(p.replace(",", ".")));
    let sec = 0;
    for (const p of parts) sec = sec * 60 + p;
    return sec;
  }

  function parseSrt(text) {
    const cues = [];
    const blocks = text.replace(/\r\n/g, "\n").split(/\n{2,}/);
    for (const block of blocks) {
      const lines = block.split("\n").filter((l) => l.trim() !== "");
      const timeIdx = lines.findIndex((l) => l.includes("-->"));
      if (timeIdx < 0) continue;
      const [start, end] = lines[timeIdx].split("-->").map(parseTimestamp);
      const body = lines.slice(timeIdx + 1).join("\n").trim();
      if (Number.isFinite(start) && Number.isFinite(end)) cues.push({ start, end, text: body });
    }
    return cues;
  }

  function parseVtt(text) {
    const cues = [];
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (line.includes("-->")) {
        const [timePart, ...rest] = line.split("-->");
        const start = parseTimestamp(timePart.trim().split(/\s+/)[0]);
        const end = parseTimestamp(rest.join("-->").trim().split(/\s+/)[0]);
        i++;
        const body = [];
        while (i < lines.length && lines[i].trim() !== "") body.push(lines[i++]);
        if (Number.isFinite(start) && Number.isFinite(end)) {
          cues.push({ start, end, text: body.join("\n").trim() });
        }
      } else {
        i++;
      }
    }
    return cues;
  }

  function parseLrc(text) {
    const cues = [];
    const tag = /\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]/g;
    for (const line of text.replace(/\r\n/g, "\n").split("\n")) {
      // Strip [mm:ss.xx] line tags and <mm:ss.xx> enhanced-LRC word-level tags.
      const body = line.replace(tag, "").replace(/<\d{1,2}:\d{2}(?:[.:]\d{1,3})?>/g, "").trim();
      if (!body) continue;
      let m;
      tag.lastIndex = 0;
      while ((m = tag.exec(line)) !== null) {
        const min = parseInt(m[1], 10);
        const sec = parseInt(m[2], 10);
        const frac = m[3] ? parseInt(m[3].padEnd(3, "0"), 10) / 1000 : 0;
        const start = min * 60 + sec + frac;
        cues.push({ start, end: start + 5, text: body });
      }
    }
    cues.sort((a, b) => a.start - b.start);
    // LRC has no end timestamp: keep each line on screen until the next line starts,
    // so longer captions stay readable instead of vanishing after a fixed 5s.
    for (let i = 0; i < cues.length; i++) {
      const next = cues[i + 1];
      cues[i].end = next ? Math.max(next.start - 0.05, cues[i].start + 1) : cues[i].start + 8;
    }
    return cues;
  }

  function parseSubtitle(text, ext) {
    if (ext === "vtt") return parseVtt(text);
    if (ext === "lrc") return parseLrc(text);
    return parseSrt(text);
  }

  function kindFor(name) {
    const ext = extOf(name);
    if (VIDEO_EXT.has(ext)) return "video";
    if (AUDIO_EXT.has(ext)) return "audio";
    if (IMAGE_EXT.has(ext)) return "image";
    if (ext === "pdf") return "pdf";
    if (TEXT_EXT.has(ext)) return "text";
    if (DOCX_EXT.has(ext)) return "docx";
    if (DOC_EXT.has(ext)) return "doc";
    if (SHEET_EXT.has(ext)) return "sheet";
    if (PPTX_EXT.has(ext)) return "pptx";
    return "unknown";
  }

  function viewerEl() {
    return document.getElementById("viewer");
  }

  function clear() {
    const el = viewerEl();
    el.innerHTML = "";
    return el;
  }

  // Close the current document: wipe the viewer and restore the empty state.
  function close() {
    if (!state.path) return;
    const el = clear();
    state.path = null;
    state.name = null;
    state.kind = null;
    state.openPath = null;
    const empty = document.createElement("div");
    empty.id = "viewer-empty";
    empty.className = "empty";
    empty.textContent = "Select a file on the left to begin";
    el.appendChild(empty);
  }

  function toast(message) {
    let t = document.getElementById("toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "toast";
      t.style.cssText =
        "position:absolute;bottom:16px;left:50%;transform:translateX(-50%);" +
        "background:var(--bg-soft-2);color:var(--fg);padding:8px 14px;border-radius:8px;" +
        "border:1px solid var(--border);z-index:10;max-width:80%;";
      viewerEl().appendChild(t);
    }
    t.textContent = message;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => t.remove(), 3000);
  }

  function setZoom(step) {
    if (step === 0) state.zoom = 1;
    else if (step > 0) state.zoom = Math.min(MAX_ZOOM, state.zoom * ZOOM_STEP);
    else state.zoom = Math.max(MIN_ZOOM, state.zoom / ZOOM_STEP);
    rerender();
  }

  function toggleFullscreen() {
    const el = viewerEl();
    if (document.fullscreenElement) document.exitFullscreen();
    else el.requestFullscreen().catch(() => toast("Cannot enter fullscreen"));
  }

  // Toolbar: filename on the left, then zoom / fullscreen / open-external actions.
  // opts.zoom / opts.fullscreen toggle the extra buttons per content kind.
  function makeToolbar(title, filePath, opts = {}) {
    const bar = document.createElement("div");
    bar.className = "viewer-toolbar";
    const label = document.createElement("span");
    label.textContent = title;
    label.style.cssText =
      "color:var(--fg-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
    bar.appendChild(label);

    const spacer = document.createElement("span");
    spacer.className = "spacer";
    bar.appendChild(spacer);

    if (opts.zoom) {
      const out = document.createElement("button");
      out.textContent = "−";
      out.title = "Zoom out";
      out.onclick = () => setZoom(-1);
      bar.appendChild(out);

      const pct = document.createElement("button");
      pct.textContent = `${Math.round(state.zoom * 100)}%`;
      pct.title = "Reset zoom";
      pct.onclick = () => setZoom(0);
      bar.appendChild(pct);

      const inn = document.createElement("button");
      inn.textContent = "+";
      inn.title = "Zoom in";
      inn.onclick = () => setZoom(1);
      bar.appendChild(inn);
    }

    if (opts.fullscreen) {
      const fs = document.createElement("button");
      fs.className = "fullscreen-btn";
      fs.textContent = "Fullscreen";
      fs.title = "Fullscreen / Exit fullscreen";
      fs.onclick = toggleFullscreen;
      bar.appendChild(fs);
    }

    // "Attach to chat" buttons: only meaningful while a real file is open (folder browse
    // has nothing to attach, and app.js must have registered an attach handler).
    if (attachHandler && state.kind !== "folder") {
      const attachBtn = document.createElement("button");
      attachBtn.textContent = "🔗";
      attachBtn.title = "Attach current file to chat";
      attachBtn.onclick = () => attachHandler("file", { path: state.openPath || state.path, name: state.name });
      bar.appendChild(attachBtn);

      const shotBtn = document.createElement("button");
      shotBtn.textContent = "📷";
      shotBtn.title = "Screenshot this window and attach to chat";
      shotBtn.onclick = () => attachHandler("screenshot");
      bar.appendChild(shotBtn);
    }

    const openBtn = document.createElement("button");
    openBtn.textContent = "Open in OS";
    openBtn.onclick = () => window.desktopAPI.openExternal(filePath);
    bar.appendChild(openBtn);

    const closeBtn = document.createElement("button");
    closeBtn.textContent = "✕";
    closeBtn.title = "Close document (Esc)";
    closeBtn.onclick = close;
    bar.appendChild(closeBtn);
    return bar;
  }

  // Apply the current subtitle style to the overlay element.
  function applySubtitleStyle(overlay) {
    overlay.style.fontSize = `${subStyle.fontSize}px`;
    overlay.style.color = subStyle.color;
    overlay.style.background = subStyle.bg ? "rgba(0,0,0,0.6)" : "transparent";
    if (subStyle.position === "top") {
      overlay.style.top = "24px";
      overlay.style.bottom = "auto";
      overlay.style.transform = "translateX(-50%)";
    } else if (subStyle.position === "middle") {
      overlay.style.top = "50%";
      overlay.style.bottom = "auto";
      overlay.style.transform = "translate(-50%, -50%)";
    } else {
      overlay.style.top = "auto";
      overlay.style.bottom = "12px";
      overlay.style.transform = "translateX(-50%)";
    }
  }

  // Show the subtitle text (or hide when empty / disabled). Timestamps are stripped
  // at parse time, so only the plain caption text is ever displayed.
  function setSubtitleText(overlay, text) {
    overlay._text = text || "";
    overlay.textContent = overlay._text;
    overlay.style.display = subStyle.enabled && overlay._text ? "" : "none";
  }

  // Inline control panel for subtitle style (size / color / background / position).
  function buildSubtitleControls(overlay) {
    const panel = document.createElement("div");
    panel.className = "subtitle-controls";

    function btn(label, onClick, active) {
      const b = document.createElement("button");
      b.textContent = label;
      b.onclick = onClick;
      if (active) b.classList.add("active");
      return b;
    }

    const sizeLabel = document.createElement("span");
    sizeLabel.textContent = `${subStyle.fontSize}px`;
    const sizeDown = btn("A−", () => {
      subStyle.fontSize = Math.max(10, subStyle.fontSize - 2);
      sizeLabel.textContent = `${subStyle.fontSize}px`;
      applySubtitleStyle(overlay);
      saveSubStyle();
    });
    const sizeUp = btn("A+", () => {
      subStyle.fontSize = Math.min(40, subStyle.fontSize + 2);
      sizeLabel.textContent = `${subStyle.fontSize}px`;
      applySubtitleStyle(overlay);
      saveSubStyle();
    });

    const colors = ["#ffffff", "#ffd54a", "#7ecb7e", "#7dd3fc", "#ff9a9a"];
    const colorRow = document.createElement("span");
    colorRow.className = "swatches";
    for (const c of colors) {
      const sw = document.createElement("button");
      sw.className = "swatch";
      sw.style.background = c;
      sw.onclick = () => {
        subStyle.color = c;
        applySubtitleStyle(overlay);
        saveSubStyle();
        colorRow.querySelectorAll(".swatch").forEach((s) => s.classList.toggle("active", s === sw));
      };
      if (c === subStyle.color) sw.classList.add("active");
      colorRow.appendChild(sw);
    }

    const bg = btn("BG", () => {
      subStyle.bg = !subStyle.bg;
      bg.classList.toggle("active", subStyle.bg);
      applySubtitleStyle(overlay);
      saveSubStyle();
    }, subStyle.bg);

    const posRow = document.createElement("span");
    posRow.className = "pos";
    for (const p of ["Top", "Middle", "Bottom"]) {
      const key = p.toLowerCase();
      const b = btn(p, () => {
        subStyle.position = key;
        applySubtitleStyle(overlay);
        saveSubStyle();
        posRow.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      }, subStyle.position === key);
      posRow.appendChild(b);
    }

    const closeBtn = btn("✕", () => {
      panel.style.display = "none";
    });
    closeBtn.title = "Close subtitle settings";
    closeBtn.style.marginLeft = "auto";
    panel.append(sizeDown, sizeLabel, sizeUp, colorRow, bg, posRow, closeBtn);
    return panel;
  }

  function renderVideo(filePath, name) {
    const el = clear();
    const bar = makeToolbar(name, filePath);
    el.appendChild(bar);

    // "Subtitles" dropdown list: Add Subtitle (pick a file) / Subtitle Settings (style panel).
    const subWrap = document.createElement("span");
    subWrap.className = "tools-wrap";
    const subBtn = document.createElement("button");
    subBtn.textContent = "Subtitles";
    const subMenu = document.createElement("div");
    subMenu.className = "tools-menu hidden";
    subWrap.append(subBtn, subMenu);
    bar.insertBefore(subWrap, bar.lastChild);

    // "Tools" dropdown: Screenshot / Generate PPT / Generate Book.
    const toolsWrap = document.createElement("span");
    toolsWrap.className = "tools-wrap";
    const toolsBtn = document.createElement("button");
    toolsBtn.textContent = "🛠 Tools";
    const toolsMenu = document.createElement("div");
    toolsMenu.className = "tools-menu hidden";
    toolsWrap.append(toolsBtn, toolsMenu);
    bar.insertBefore(toolsWrap, bar.lastChild);

    toolsBtn.onclick = (e) => {
      e.stopPropagation();
      toolsMenu.classList.toggle("hidden");
    };
    document.addEventListener("click", () => toolsMenu.classList.add("hidden"));

    const body = document.createElement("div");
    body.className = "viewer-body";
    const wrap = document.createElement("div");
    wrap.className = "video-wrap";
    const video = document.createElement("video");
    video.controls = true;
    video.crossOrigin = "anonymous"; // so the canvas can read frames
    video.src = localUrl(filePath);
    const overlay = document.createElement("div");
    overlay.className = "subtitle-overlay";
    applySubtitleStyle(overlay);
    overlay.style.display = "none";
    wrap.appendChild(video);
    wrap.appendChild(overlay);
    body.appendChild(wrap);

    const controls = buildSubtitleControls(overlay);
    controls.style.display = "none";

    function subMenuItem(label, onClick) {
      const b = document.createElement("button");
      b.textContent = label;
      b.onclick = () => {
        subMenu.classList.add("hidden");
        onClick();
      };
      subMenu.appendChild(b);
    }
    subMenuItem("Enable Subtitles", () => {
      subStyle.enabled = true;
      setSubtitleText(overlay, overlay._text || "");
      saveSubStyle();
    });
    subMenuItem("Disable Subtitles", () => {
      subStyle.enabled = false;
      setSubtitleText(overlay, overlay._text || "");
      saveSubStyle();
    });
    subMenuItem("Add Subtitle", chooseSubtitle);
    subMenuItem("Subtitle Settings", () => {
      controls.style.display = controls.style.display === "none" ? "flex" : "none";
    });
    subBtn.onclick = (e) => {
      e.stopPropagation();
      subMenu.classList.toggle("hidden");
    };
    document.addEventListener("click", () => subMenu.classList.add("hidden"));

    el.appendChild(controls);
    el.appendChild(body);

    // ── Subtitles: auto-detect a sibling file, or let the user pick one ──
    let cues = [];
    video.addEventListener("timeupdate", () => {
      const t = video.currentTime;
      const cue = cues.find((c) => t >= c.start && t <= c.end);
      setSubtitleText(overlay, cue ? cue.text : "");
    });

    async function loadSubtitleFrom(subPath) {
      try {
        const res = await window.desktopAPI.readText(subPath);
        if (!res.ok) {
          toast(`Subtitle load failed: ${res.error}`);
          return;
        }
        const parsed = parseSubtitle(res.content, extOf(subPath));
        if (!parsed.length) {
          toast("No subtitle cues found in file.");
          return;
        }
        cues = parsed;
        toast(`Subtitles: ${subPath.split(/[\\/]/).pop()}`);
      } catch (err) {
        toast(`Subtitle load failed: ${err.message}`);
      }
    }

    function chooseSubtitle() {
      // Open the picker in the video's folder by default (subtitle usually sits next to it).
      window.desktopAPI.pickSubtitle(dirOf(filePath)).then((picked) => {
        if (picked) loadSubtitleFrom(picked);
      });
    }

    (async () => {
      let auto;
      try {
        auto = await window.desktopAPI.findSubtitle(filePath);
      } catch {
        auto = null;
      }
      if (auto) await loadSubtitleFrom(auto);
    })();

    function menuItem(label, onClick) {
      const b = document.createElement("button");
      b.textContent = label;
      b.onclick = () => {
        toolsMenu.classList.add("hidden");
        onClick();
      };
      toolsMenu.appendChild(b);
    }

    menuItem("📷 Screenshot", async () => {
      if (!video.videoWidth) {
        toast("Video not ready yet");
        return;
      }
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
      const dataURL = canvas.toDataURL("image/png");
      const saved = await window.desktopAPI.saveScreenshot(dataURL, `${baseName(name)}.png`);
      toast(saved ? `Saved: ${saved}` : "Cancelled");
    });
    menuItem("Generate PPT", () => generateMedia("pptx"));
    menuItem("Generate Book", () => generateMedia("pdf"));

    function generateMedia(format) {
      if (typeof window.generateMedia === "function") {
        window.generateMedia(filePath, name, format);
      } else {
        toast("Generation not available yet");
      }
    }
  }

  function renderAudio(filePath, name) {
    const el = clear();
    el.appendChild(makeToolbar(name, filePath));
    const body = document.createElement("div");
    body.className = "viewer-body";
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = localUrl(filePath);
    body.appendChild(audio);
    el.appendChild(body);
  }

  function renderImage(filePath, name) {
    const el = clear();
    el.appendChild(makeToolbar(name, filePath, { zoom: true, fullscreen: true }));
    const body = document.createElement("div");
    body.className = "viewer-body";
    body.style.overflow = "auto";
    const img = document.createElement("img");
    img.src = localUrl(filePath);
    img.style.margin = "auto"; // margin:auto centers but stays scrollable when zoomed
    img.onload = () => {
      img.style.transform = `scale(${state.zoom})`;
    };
    body.appendChild(img);
    el.appendChild(body);
  }

  async function renderPdf(filePath, name) {
    clear();
    const el = viewerEl();
    const bar = makeToolbar(name, state.openPath || filePath, { zoom: true, fullscreen: true });
    el.appendChild(bar);
    const container = document.createElement("div");
    container.className = "pdf-container";
    el.appendChild(container);

    if (!window.pdfjsLib) {
      renderFallback(state.openPath || filePath, name, "PDF component not loaded. Reinstall dependencies.");
      return;
    }

    // Load annotations once per PDF (zoom re-renders reuse the in-memory copy).
    if (annotPath !== filePath) {
      annotPath = filePath;
      annotations = { strokes: [], notes: [] };
      liveStroke = null;
      try {
        const ares = await window.desktopAPI.readAnnotations(filePath);
        if (ares.ok && ares.data) annotations = ares.data;
      } catch { /* no sidecar yet */ }
    }

    try {
      const pdfjs = window.pdfjsLib;
      pdfjs.GlobalWorkerOptions.workerSrc = "vendor/pdf.worker.min.js";
      const pdf = await pdfjs.getDocument(localUrl(filePath)).promise;
      const scale = 1.5 * state.zoom;
      const pageCanvases = [];
      const pageOverlays = [];
      const pageWraps = [];

      function saveAnnotations() {
        if (annotPath) window.desktopAPI.saveAnnotations(annotPath, annotations).catch(() => {});
      }

      function strokePath(ctx, s, w, h) {
        ctx.strokeStyle = s.color;
        ctx.lineWidth = s.width;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        if (s.points.length < 2) {
          const p = s.points[0];
          ctx.fillStyle = s.color;
          ctx.beginPath();
          ctx.arc(p.x * w, p.y * h, s.width / 2, 0, Math.PI * 2);
          ctx.fill();
          return;
        }
        ctx.beginPath();
        ctx.moveTo(s.points[0].x * w, s.points[0].y * h);
        for (let i = 1; i < s.points.length; i++) ctx.lineTo(s.points[i].x * w, s.points[i].y * h);
        ctx.stroke();
      }

      function renderNoteMarkers(pageIndex) {
        const wrap = pageWraps[pageIndex];
        if (!wrap) return;
        wrap.querySelectorAll(".pdf-note").forEach((n) => n.remove());
        for (const note of annotations.notes) {
          if (note.page !== pageIndex) continue;
          const marker = document.createElement("div");
          marker.className = "pdf-note";
          marker.style.left = `${note.x * 100}%`;
          marker.style.top = `${note.y * 100}%`;
          marker.title = note.text || "";
          marker.textContent = "🗒";
          marker.addEventListener("click", (e) => {
            e.stopPropagation();
            editNote(note, pageIndex);
          });
          wrap.appendChild(marker);
        }
      }

      function redrawPage(pageIndex) {
        const overlay = pageOverlays[pageIndex];
        if (!overlay) return;
        const ctx = overlay.getContext("2d");
        ctx.clearRect(0, 0, overlay.width, overlay.height);
        for (const s of annotations.strokes) {
          if (s.page !== pageIndex) continue;
          strokePath(ctx, s, overlay.width, overlay.height);
        }
        if (liveStroke && liveStroke.page === pageIndex) {
          strokePath(ctx, liveStroke, overlay.width, overlay.height);
        }
        renderNoteMarkers(pageIndex);
      }

      function editNote(note, pageIndex) {
        el.querySelectorAll(".pdf-note-editor").forEach((x) => x.remove());
        const wrap = pageWraps[pageIndex];
        const editor = document.createElement("div");
        editor.className = "pdf-note-editor";
        editor.style.left = `${note.x * 100}%`;
        editor.style.top = `${note.y * 100}%`;
        const ta = document.createElement("textarea");
        ta.placeholder = "Add a note…";
        ta.value = note.text || "";
        const saveBtn = document.createElement("button");
        saveBtn.textContent = "Save";
        const delBtn = document.createElement("button");
        delBtn.textContent = "Delete";
        editor.append(ta, saveBtn, delBtn);
        wrap.appendChild(editor);
        ta.focus();
        saveBtn.onclick = () => {
          note.text = ta.value.trim();
          saveAnnotations();
          editor.remove();
          renderNoteMarkers(pageIndex);
        };
        delBtn.onclick = () => {
          annotations.notes = annotations.notes.filter((n) => n !== note);
          saveAnnotations();
          editor.remove();
          renderNoteMarkers(pageIndex);
        };
      }

      function placeNote(pageIndex, pt) {
        const note = { page: pageIndex, x: pt.x, y: pt.y, text: "" };
        annotations.notes.push(note);
        saveAnnotations();
        renderNoteMarkers(pageIndex);
        editNote(note, pageIndex);
      }

      function eraseAt(pt, pageIndex) {
        const before = annotations.strokes.length;
        annotations.strokes = annotations.strokes.filter((s) => {
          if (s.page !== pageIndex) return true;
          return !s.points.some((p) => Math.hypot(p.x - pt.x, p.y - pt.y) < 0.04);
        });
        if (annotations.strokes.length !== before) {
          saveAnnotations();
          redrawPage(pageIndex);
        }
      }

      function normPoint(e, overlay) {
        const r = overlay.getBoundingClientRect();
        return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
      }

      function wireOverlay(overlay, pageIndex) {
        overlay.addEventListener("pointerdown", (e) => {
          if (annot.tool === "draw") {
            liveStroke = { page: pageIndex, color: annot.color, width: annot.width, points: [normPoint(e, overlay)] };
            overlay.setPointerCapture(e.pointerId);
            redrawPage(pageIndex);
          } else if (annot.tool === "erase") {
            eraseAt(normPoint(e, overlay), pageIndex);
          } else if (annot.tool === "note") {
            placeNote(pageIndex, normPoint(e, overlay));
          }
        });
        overlay.addEventListener("pointermove", (e) => {
          if (!liveStroke || liveStroke.page !== pageIndex) return;
          liveStroke.points.push(normPoint(e, overlay));
          redrawPage(pageIndex);
        });
        overlay.addEventListener("pointerup", () => {
          if (!liveStroke || liveStroke.page !== pageIndex) return;
          if (liveStroke.points.length) {
            annotations.strokes.push(liveStroke);
            saveAnnotations();
          }
          liveStroke = null;
          redrawPage(pageIndex);
        });
      }

      for (let i = 1; i <= pdf.numPages; i++) {
        const page = await pdf.getPage(i);
        const viewport = page.getViewport({ scale });
        const canvas = document.createElement("canvas");
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;

        const wrap = document.createElement("div");
        wrap.className = "pdf-page";
        wrap.appendChild(canvas);

        const textLayer = document.createElement("div");
        textLayer.className = "pdf-text-layer";
        wrap.appendChild(textLayer);
        try {
          const textContent = await page.getTextContent();
          const textTask = pdfjs.renderTextLayer({
            textContentSource: textContent,
            container: textLayer,
            viewport,
            textDivs: [],
          });
          if (textTask && textTask.promise) await textTask.promise;
        } catch { /* text layer is best-effort */ }

        const overlay = document.createElement("canvas");
        overlay.className = "pdf-overlay";
        overlay.width = viewport.width;
        overlay.height = viewport.height;
        wrap.appendChild(overlay);
        container.appendChild(wrap);

        const pageIndex = i - 1;
        pageCanvases.push(canvas);
        pageOverlays.push(overlay);
        pageWraps.push(wrap);
        wireOverlay(overlay, pageIndex);
        redrawPage(pageIndex);
      }

      // ── Annotation toolbar ──
      function syncAnnotToolbar() {
        bar.querySelectorAll(".annot-tool").forEach((x) => {
          x.classList.toggle("active", x._tool === annot.tool);
        });
        const active = annot.tool !== "none";
        for (const o of pageOverlays) {
          o.style.cursor = active ? "crosshair" : "default";
          o.style.pointerEvents = active ? "auto" : "none";
        }
      }
      function annotToolBtn(label, tool) {
        const b = document.createElement("button");
        b.textContent = label;
        b.onclick = () => {
          annot.tool = annot.tool === tool ? "none" : tool;
          syncAnnotToolbar();
        };
        b._tool = tool;
        b.classList.add("annot-tool");
        return b;
      }
      const selectBtn = document.createElement("button");
      selectBtn.textContent = "Select";
      selectBtn.title = "Exit annotation mode (browse / select)";
      selectBtn.classList.add("annot-tool");
      selectBtn._tool = "none";
      selectBtn.onclick = () => { annot.tool = "none"; syncAnnotToolbar(); };

      const drawBtn = annotToolBtn("Draw", "draw");
      const eraseBtn = annotToolBtn("Erase", "erase");
      const noteBtn = annotToolBtn("Note", "note");

      const colorInput = document.createElement("input");
      colorInput.type = "color";
      colorInput.value = annot.color;
      colorInput.title = "Pen color";
      colorInput.oninput = () => { annot.color = colorInput.value; };

      const widthInput = document.createElement("input");
      widthInput.type = "range";
      widthInput.min = "1";
      widthInput.max = "12";
      widthInput.value = String(annot.width);
      widthInput.title = "Pen width";
      widthInput.oninput = () => { annot.width = parseInt(widthInput.value, 10); };

      const clearBtn = document.createElement("button");
      clearBtn.textContent = "Clear";
      clearBtn.title = "Remove all annotations";
      clearBtn.onclick = () => {
        annotations = { strokes: [], notes: [] };
        saveAnnotations();
        for (let i = 0; i < pageOverlays.length; i++) redrawPage(i);
      };

      const exportBtn = document.createElement("button");
      exportBtn.textContent = "Export PDF";
      exportBtn.title = "Flatten annotations into a copy of the PDF";
      exportBtn.onclick = async () => {
        if (!annotPath) return;
        exportBtn.disabled = true;
        exportBtn.textContent = "…";
        try {
          const res = await window.desktopAPI.embedAnnotations(annotPath, annotations);
          if (res.ok) {
            toast(`Exported: ${res.path}`);
            window.desktopAPI.openExternal(res.path);
          } else {
            toast(`Export failed: ${res.error}`);
          }
        } catch (err) {
          toast(`Export failed: ${err.message}`);
        } finally {
          exportBtn.disabled = false;
          exportBtn.textContent = "Export PDF";
        }
      };

      const spacer = bar.querySelector(".spacer");
      bar.insertBefore(clearBtn, spacer);
      bar.insertBefore(exportBtn, spacer);
      bar.insertBefore(widthInput, spacer);
      bar.insertBefore(colorInput, spacer);
      bar.insertBefore(noteBtn, spacer);
      bar.insertBefore(eraseBtn, spacer);
      bar.insertBefore(drawBtn, spacer);
      bar.insertBefore(selectBtn, spacer);
      syncAnnotToolbar();

      // ── Outline (bookmarks) → click-to-navigate panel ──
      const outline = await pdf.getOutline();
      if (outline && outline.length) {
        const panel = document.createElement("div");
        panel.className = "pdf-toc hidden";
        el.appendChild(panel);

        function tocNode(item, depth) {
          const wrap = document.createElement("div");
          wrap.className = "pdf-toc-item";
          wrap.style.paddingLeft = `${depth * 10 + 4}px`;

          const row = document.createElement("div");
          row.className = "pdf-toc-row";
          const arrow = document.createElement("span");
          arrow.className = "pdf-toc-arrow";
          const label = document.createElement("span");
          label.className = "pdf-toc-label";
          label.textContent = item.title;
          row.append(arrow, label);
          wrap.appendChild(row);

          const kids = item.items && item.items.length ? item.items : null;
          if (kids) {
            arrow.textContent = "▸";
            const childrenBox = document.createElement("div");
            childrenBox.className = "pdf-toc-children";
            for (const c of kids) childrenBox.appendChild(tocNode(c, depth + 1));
            wrap.appendChild(childrenBox);
            arrow.addEventListener("click", (e) => {
              e.stopPropagation();
              const open = childrenBox.style.display !== "none";
              childrenBox.style.display = open ? "none" : "block";
              arrow.textContent = open ? "▸" : "▾";
            });
          }

          if (item.dest) {
            label.addEventListener("click", async () => {
              try {
                const dest = await pdf.getDestination(item.dest);
                if (!dest) return;
                const idx = await pdf.getPageIndex(dest[0]);
                const wrap = pageWraps[idx];
                if (!wrap) return;
                const top = wrap.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
                container.scrollTo({ top, behavior: "smooth" });
              } catch { /* ignore unresolvable destination */ }
            });
          }
          return wrap;
        }
        for (const item of outline) panel.appendChild(tocNode(item, 0));

        const tocBtn = document.createElement("button");
        tocBtn.textContent = "Contents";
        tocBtn.title = "Show / hide document outline";
        tocBtn.onclick = () => panel.classList.toggle("hidden");
        bar.insertBefore(tocBtn, spacer);
      }
    } catch (err) {
      renderFallback(state.openPath || filePath, name, `PDF render failed: ${err.message}`);
    }
  }

  // Load a file's bytes (Uint8Array) via IPC for the binary Office viewers.
  async function readBytes(filePath) {
    const res = await window.desktopAPI.readFileBytes(filePath);
    if (!res.ok) throw new Error(res.error || "Failed to read file.");
    if (!res.data || res.data.byteLength === 0) {
      throw new Error("This file is empty (0 bytes) — it may have failed to upload.");
    }
    return res.data;
  }

  // Strip anything dangerous from HTML produced by mammoth (a docx can embed links and
  // raw HTML). Blocks script/iframe/object/etc., drops on* handlers, and allows only safe
  // link schemes (data: URLs are kept for images mammoth inlines, blocked otherwise).
  function sanitizeHtml(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const FORBIDDEN = new Set(["script", "iframe", "object", "embed", "base", "link", "meta", "form", "style"]);
    doc.querySelectorAll("*").forEach((n) => {
      if (FORBIDDEN.has(n.tagName.toLowerCase())) { n.remove(); return; }
      [...n.attributes].forEach((a) => {
        const name = a.name.toLowerCase();
        if (name.startsWith("on")) { n.removeAttribute(a.name); return; }
        if (name === "href" || name === "src") {
          const v = a.value.trim();
          if (/^(javascript:|vbscript:)/i.test(v)) n.removeAttribute(a.name);
          else if (name === "href" && /^data:/i.test(v)) n.removeAttribute(a.name);
          else if (name === "src" && /^data:/i.test(v) && !/^data:image\//i.test(v)) n.removeAttribute(a.name);
        }
      });
    });
    return doc.body.innerHTML;
  }

  // Word (.docx): render with the vendored mammoth (docx → HTML), sanitized before display.
  async function renderDocx(filePath, name) {
    const el = clear();
    el.appendChild(makeToolbar(name, filePath, { fullscreen: true }));
    const body = document.createElement("div");
    body.className = "docx-viewer";
    el.appendChild(body);
    try {
      const data = await readBytes(filePath);
      if (typeof mammoth === "undefined") throw new Error("mammoth not loaded.");
      const ab = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
      const result = await mammoth.convertToHtml({ arrayBuffer: ab, includeRawHtml: false });
      body.innerHTML = sanitizeHtml(result.value);
    } catch (err) {
      body.innerHTML = "";
      renderFallback(filePath, name, `Preview failed: ${err.message}`);
    }
  }

  // Legacy binary Word (.doc): extract text + embedded images with word-extractor in the
  // main process. Formatting/layout don't survive — paragraphs are shown one per line and
  // raster images (PNG/JPEG/GIF/BMP) render below them, best-effort and unpositioned.
  // Unparseable .doc files fall back to the OS default app.
  async function renderDoc(filePath, name) {
    const el = clear();
    el.appendChild(makeToolbar(name, filePath, { fullscreen: true }));
    const body = document.createElement("div");
    body.className = "docx-viewer";
    el.appendChild(body);
    try {
      const res = await window.desktopAPI.extractWordText(filePath);
      if (!res.ok) throw new Error(res.error || "Failed to extract text.");
      const frag = document.createDocumentFragment();
      for (const line of String(res.content || "").split(/\r?\n/)) {
        const t = line.replace(/\t/g, "    ").trim();
        if (!t) continue;
        const p = document.createElement("p");
        p.textContent = t;
        frag.append(p);
      }
      if (!frag.childNodes.length && !(res.images && res.images.length)) {
        throw new Error("The document contains no extractable text or images.");
      }
      if (res.images && res.images.length) {
        const note = document.createElement("p");
        note.className = "doc-extract-note";
        note.textContent =
          `${res.images.length} embedded image(s) below — best-effort, not positioned as in Word.`;
        frag.append(note);
        for (const im of res.images) {
          const img = document.createElement("img");
          img.className = "doc-extract-img";
          img.src = `data:${im.mime};base64,${im.b64}`;
          frag.append(img);
        }
      }
      body.appendChild(frag);
    } catch (err) {
      body.innerHTML = "";
      renderFallback(filePath, name, `Preview failed: ${err.message}`);
    }
  }

  // Excel (.xlsx/.xls) and delimited text (.csv/.tsv): parse with vendored SheetJS, render
  // each sheet as a table with tabs. CSV/TSV are decoded to a string first so SheetJS
  // auto-detects the delimiter (comma / tab) instead of treating the bytes as a zip.
  async function renderSheet(filePath, name) {
    const el = clear();
    el.appendChild(makeToolbar(name, filePath, { fullscreen: true }));
    const body = document.createElement("div");
    body.className = "sheet-viewer";
    el.appendChild(body);
    try {
      const data = await readBytes(filePath);
      if (typeof XLSX === "undefined") throw new Error("SheetJS (xlsx) not loaded.");
      const ext = extOf(name);
      const wb = ext === "csv" || ext === "tsv"
        ? XLSX.read(new TextDecoder("utf-8").decode(data), { type: "string" })
        : XLSX.read(data, { type: "array" });
      if (!wb.SheetNames.length) throw new Error("No sheets in workbook.");
      const tabs = document.createElement("div");
      tabs.className = "sheet-tabs";
      const panels = document.createElement("div");
      panels.className = "sheet-panels";
      wb.SheetNames.forEach((sname, i) => {
        const tab = document.createElement("button");
        tab.textContent = sname;
        tab.className = i === 0 ? "active" : "";
        tab.onclick = () => {
          tabs.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
          tab.classList.add("active");
          panels.innerHTML = XLSX.utils.sheet_to_html(wb.Sheets[sname]);
        };
        tabs.appendChild(tab);
      });
      body.append(tabs, panels);
      panels.innerHTML = XLSX.utils.sheet_to_html(wb.Sheets[wb.SheetNames[0]]);
    } catch (err) {
      body.innerHTML = "";
      renderFallback(filePath, name, `Preview failed: ${err.message}`);
    }
  }

  // PowerPoint (.pptx): parse the ZIP with JSZip and lay out slides via PptxViewer.
  async function renderPptx(filePath, name) {
    const el = clear();
    el.appendChild(makeToolbar(name, filePath, { fullscreen: true }));
    const body = document.createElement("div");
    body.className = "pptx-wrap";
    el.appendChild(body);
    try {
      const data = await readBytes(filePath);
      const ab = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
      await PptxViewer.render(ab, body);
    } catch (err) {
      body.innerHTML = "";
      renderFallback(filePath, name, `Preview failed: ${err.message}`);
    }
  }

  async function renderText(filePath, name) {
    clear();
    const el = viewerEl();
    el.appendChild(makeToolbar(name, filePath, { zoom: true, fullscreen: true }));
    const res = await window.desktopAPI.readText(filePath);
    if (!res.ok) {
      renderFallback(filePath, name, res.error);
      return;
    }
    const pre = document.createElement("pre");
    pre.className = "text-viewer";
    pre.style.fontSize = `${13 * state.zoom}px`;
    pre.textContent = res.content;
    el.appendChild(pre);
  }

  function renderFallback(filePath, name, note) {
    const el = clear();
    const box = document.createElement("div");
    box.className = "fallback";
    const title = document.createElement("div");
    title.className = "name";
    title.textContent = name;
    const p = document.createElement("p");
    p.textContent = note || "This format can't be previewed in-window. Open with the system default app.";
    const btn = document.createElement("button");
    btn.textContent = "Open with system app";
    btn.onclick = () => window.desktopAPI.openExternal(filePath);
    box.append(title, p, btn);
    el.appendChild(box);
  }

  function dispatch(kind, filePath, name) {
    switch (kind) {
      case "video": return renderVideo(filePath, name);
      case "audio": return renderAudio(filePath, name);
      case "image": return renderImage(filePath, name);
      case "pdf": return renderPdf(filePath, name);
      case "docx": return renderDocx(filePath, name);
      case "doc": return renderDoc(filePath, name);
      case "sheet": return renderSheet(filePath, name);
      case "pptx": return renderPptx(filePath, name);
      case "text": return renderText(filePath, name);
      default: return renderFallback(filePath, name, null);
    }
  }

  function render(filePath, name) {
    state.path = filePath;
    state.name = name;
    state.kind = kindFor(name);
    state.openPath = filePath;
    return dispatch(state.kind, filePath, name);
  }

  // Re-render the current view at the current zoom (used by the zoom buttons).
  function rerender() {
    if (!state.path) return;
    if (state.kind === "folder") return; // folder browse has no zoom
    dispatch(state.kind, state.path, state.name);
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

  function span(className, text) {
    const s = document.createElement("span");
    if (className) s.className = className;
    if (text != null) s.textContent = text;
    return s;
  }

  // Folder-browse view: list a folder's children in the main area with breadcrumb
  // navigation. `entries` are opaque to the viewer — dir entries must carry
  // { type: "dir", name, path }, file entries are passed whole to `open`.
  // `opts`:
  //   rootPath   browse-root folder path (workspaceDir for local, "" for cloud)
  //   rootName   display name of the root (workspace folder name / "My Drive")
  //   path       current folder path relative to root ("" = root)
  //   read       async (relPath) => { entries, localPath } — children of a folder
  //   open       (entry) => void — source-specific file opener
  //   localPath  current folder's OS absolute path (null hides "Open in OS")
  async function renderFolder(entries, opts) {
    const { rootPath, rootName, path = "", read, open, localPath = null } = opts;
    const segs = path ? path.split("/").filter(Boolean) : [];
    state.path = path || rootPath || rootName;
    state.name = segs.length ? segs[segs.length - 1] : rootName;
    state.kind = "folder";

    const el = clear();

    // The cloud drive view renders its own toolbar (view toggle / search / actions),
    // so the plain local-file toolbar is skipped there.
    if (!opts.cloudTable) {
      const bar = makeToolbar(state.name, localPath);
      if (!localPath) {
        const osBtn = Array.from(bar.querySelectorAll("button")).find(
          (b) => b.textContent === "Open in OS"
        );
        if (osBtn) osBtn.remove();
      }
      el.appendChild(bar);
    }

    // Breadcrumb: root crumb + each ancestor segment; clicking navigates up.
    const crumbBar = document.createElement("div");
    crumbBar.className = "folder-breadcrumb";
    const crumbs = [{ label: rootName, relPath: "" }];
    let acc = "";
    for (const s of segs) {
      acc = acc ? `${acc}/${s}` : s;
      crumbs.push({ label: s, relPath: acc });
    }
    for (let i = 0; i < crumbs.length; i++) {
      if (i > 0) {
        const sep = span("cd-crumb-sep", "›");
        crumbBar.appendChild(sep);
      }
      const c = document.createElement("button");
      c.className = "cd-crumb" + (i === crumbs.length - 1 ? " current" : "");
      c.textContent = crumbs[i].label;
      c.title = `Open folder ${crumbs[i].relPath || rootName}`;
      if (i < crumbs.length - 1) {
        c.onclick = async () => {
          if (opts.onCrumb) {
            opts.onCrumb(crumbs[i].relPath);
            return;
          }
          try {
            const res = await read(crumbs[i].relPath);
            renderFolder(res.entries, {
              ...opts,
              path: crumbs[i].relPath,
              localPath: res.localPath,
            });
          } catch (err) {
            toast(`Cannot open folder: ${err.message || err}`);
          }
        };
      }
      crumbBar.appendChild(c);
    }
    el.appendChild(crumbBar);

    // Cloud drive (web CloudDrive parity): the clouddrive.js owner drives a
    // rich table/grid renderer instead of the simple local-folder list below.
    if (opts.cloudTable) return renderCloudBody(el, entries, opts);

    // List body: folders first, then files (reuse the cloud-drive row styles).
    const body = document.createElement("div");
    body.className = "folder-body";
    const folders = (entries || []).filter((e) => e.type === "dir");
    const files = (entries || []).filter((e) => e.type !== "dir");
    if (!folders.length && !files.length) {
      const empty = document.createElement("div");
      empty.className = "folder-empty";
      empty.textContent = "Empty folder.";
      body.appendChild(empty);
    } else {
      for (const d of folders) {
        const row = document.createElement("div");
        row.className = "cd-row cd-folder";
        row.title = `Browse ${d.name}`;
        row.appendChild(span("cd-icon", "📁"));
        row.appendChild(span("cd-name", d.name));
        row.addEventListener("click", async () => {
          try {
            const res = await read(d.path);
            renderFolder(res.entries, {
              ...opts,
              path: d.path,
              localPath: res.localPath,
            });
          } catch (err) {
            toast(`Cannot open folder: ${err.message || err}`);
          }
        });
        body.appendChild(row);
      }
      for (const f of files) {
        const row = document.createElement("div");
        row.className = "cd-row cd-file";
        row.title = f.name;
        row.appendChild(span("cd-icon", "📄"));
        row.appendChild(span("cd-name", f.name));
        if (f.size != null) row.appendChild(span("cd-meta", fmtSize(f.size)));
        // Optional per-file trailing widget (e.g. a RAG status badge / import button).
        if (opts.rowAction) {
          const act = opts.rowAction(f);
          if (act) row.appendChild(act);
        }
        row.addEventListener("click", () => open(f));
        body.appendChild(row);
      }
    }
    el.appendChild(body);
  }

  // Cloud drive parity view (web CloudDrive.tsx): toolbar + (edit-mode) batch
  // bar + list/grid body. Pure renderer — every interaction routes back through
  // the opts callbacks so clouddrive.js stays the single owner of state.
  function renderCloudBody(el, entries, opts) {
    const { cloud, onAction, onSearch, onEnterFolder, onOpenEntry, onToggleOne, onToggleAll, onDeleteFolder, onBatch, ragCell, ragBadge } = opts;
    const { viewMode, editMode, isTrash, canWrite, canManage, inWs, trashCount, selected, query, locLabel, emptyText } = cloud;

    const dirs = (entries || []).filter((e) => e.type === "dir");
    const files = (entries || []).filter((e) => e.type !== "dir");

    function btn(label, title, onClick, cls = "", disabled = false) {
      const b = document.createElement("button");
      b.textContent = label;
      b.title = title;
      if (cls) b.className = cls;
      b.disabled = disabled;
      if (onClick) b.onclick = onClick;
      return b;
    }
    function thEl(text) {
      const th = document.createElement("th");
      th.textContent = text;
      return th;
    }
    function tdEl(children) {
      const td = document.createElement("td");
      if (Array.isArray(children)) children.forEach((c) => td.appendChild(c));
      else if (children) td.appendChild(children);
      return td;
    }

    // ---- Toolbar ----
    const tool = document.createElement("div");
    tool.className = "cdt-toolbar";
    const pathLab = span("cdt-path", locLabel);
    pathLab.title = locLabel;
    tool.appendChild(pathLab);

    const vt = document.createElement("span");
    vt.className = "cdt-view-toggle";
    vt.appendChild(btn("☰", "List view", () => onAction("view-list"), viewMode === "list" ? "active" : ""));
    vt.appendChild(btn("▦", "Grid view", () => onAction("view-grid"), viewMode === "grid" ? "active" : ""));
    tool.appendChild(vt);

    if (!isTrash) {
      const sw = document.createElement("span");
      sw.className = "cdt-search-wrap";
      const inp = document.createElement("input");
      inp.className = "cdt-search";
      inp.placeholder = "Search files…";
      inp.value = query || "";
      inp.addEventListener("input", () => onSearch(inp.value));
      sw.appendChild(inp);
      tool.appendChild(sw);
    }

    tool.appendChild(span("cdt-spacer", ""));

    if (inWs && canManage) {
      tool.appendChild(btn("⚙ Manage", "Manage workspace members and settings", () => onAction("manage")));
    }
    tool.appendChild(btn(editMode ? "✓ Done" : "✏ Edit", editMode ? "Done selecting" : "Show selection checkboxes", () => onAction("edit")));
    if (isTrash) {
      tool.appendChild(btn("Empty Trash", "Permanently delete everything in Trash", () => onAction("empty-trash"), "danger", trashCount === 0));
    } else {
      if (canWrite) tool.appendChild(btn("＋ New folder", "Create a new folder here", () => onAction("new-folder")));
      if (canWrite) tool.appendChild(btn("＋ New text", "Create a new text file here", () => onAction("new-text")));
      if (canWrite) tool.appendChild(btn("⬆ Upload", "Upload a file here", () => onAction("upload"), "primary"));
    }
    el.appendChild(tool);

    // ---- Edit-mode batch bar ----
    if (editMode) {
      const bb = document.createElement("div");
      bb.className = "cdt-batchbar";
      const count = files.filter((f) => selected.has(f.id)).length;
      bb.appendChild(span("cdt-batch-count", `${count} selected`));
      const add = (label, title, fn, cls, disabled) => bb.appendChild(btn(label, title, fn, cls, disabled));
      if (isTrash) {
        add("↩ Restore", "Restore selected files", () => onBatch("restore"), "", count === 0);
        add("✖ Delete permanently", "Permanently delete selected files", () => onBatch("purge"), "danger", count === 0);
      } else {
        add("⬇ Download", "Download selected files", () => onBatch("download"), "", count === 0);
        add("↗ Open", "Open the selected file", () => onBatch("open"), "", count !== 1);
        add("🔗 Share", "Share the selected file", () => onBatch("share"), "", count !== 1 || !canWrite);
        add("✏ Rename", "Rename the selected file", () => onBatch("rename"), "", count !== 1 || !canWrite);
        add("⇄ Move", "Move selected files to another folder or workspace", () => onBatch("move"), "", count === 0 || !canWrite);
        add("🗑 Delete", "Move selected files to Trash", () => onBatch("delete"), "danger", count === 0 || !canWrite);
      }
      el.appendChild(bb);
    }

    // ---- Body ----
    const body = document.createElement("div");
    body.className = "cdt-body";

    if (!dirs.length && !files.length) {
      const empty = document.createElement("div");
      empty.className = "cdt-empty";
      empty.textContent = emptyText;
      body.appendChild(empty);
    } else if (viewMode === "grid") {
      const grid = document.createElement("div");
      grid.className = "cdt-grid";
      for (const d of dirs) {
        const tile = document.createElement("div");
        tile.className = "cdt-tile cdt-dir-tile";
        tile.title = "Double-click to enter";
        tile.addEventListener("dblclick", () => onEnterFolder(d));
        tile.appendChild(span("cdt-tile-icon", "📁"));
        tile.appendChild(span("cdt-tile-name", d.name));
        const meta = document.createElement("div");
        meta.className = "cdt-tile-meta";
        meta.appendChild(span("muted", "Folder"));
        tile.appendChild(meta);
        grid.appendChild(tile);
      }
      for (const f of files) {
        const tile = document.createElement("div");
        tile.className = "cdt-tile" + (selected.has(f.id) ? " selected" : "");
        tile.title = editMode ? (selected.has(f.id) ? "Click to deselect" : "Click to select") : "Click to open";
        tile.addEventListener("click", () => onOpenEntry(f));
        if (editMode) {
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.className = "cdt-tile-check";
          cb.checked = selected.has(f.id);
          cb.addEventListener("click", (e) => e.stopPropagation());
          cb.addEventListener("change", () => onToggleOne(f.id));
          tile.appendChild(cb);
        }
        tile.appendChild(span("cdt-tile-icon", "📄"));
        tile.appendChild(span("cdt-tile-name", f.name));
        const meta = document.createElement("div");
        meta.className = "cdt-tile-meta";
        if (isTrash) {
          meta.appendChild(span("muted", fmtDate(f.deleted_at)));
        } else {
          meta.appendChild(span("muted", fmtSize(f.size)));
          const b = ragBadge ? ragBadge(f) : null;
          if (b) meta.appendChild(b);
        }
        tile.appendChild(meta);
        grid.appendChild(tile);
      }
      body.appendChild(grid);
    } else {
      // List view: folder rows on top, then the file table.
      if (dirs.length) {
        const dl = document.createElement("div");
        dl.className = "cdt-dir-list";
        for (const d of dirs) {
          const row = document.createElement("div");
          row.className = "cdt-dir-row";
          row.title = "Double-click to enter";
          row.addEventListener("dblclick", () => onEnterFolder(d));
          row.appendChild(span("cdt-folder-icon", "📁"));
          row.appendChild(span("cdt-node-name", d.name));
          row.appendChild(span("cdt-flex", ""));
          if (editMode) {
            const del = btn("🗑", canWrite ? "Delete folder (its files move to Trash)" : "You don't have write access here", () => onDeleteFolder(d), "cdt-danger", !canWrite);
            row.appendChild(del);
          }
          dl.appendChild(row);
        }
        body.appendChild(dl);
      }

      const table = document.createElement("table");
      table.className = "cdt-table";
      const thead = document.createElement("thead");
      const trh = document.createElement("tr");
      if (editMode) {
        const th = document.createElement("th");
        th.style.width = "32px";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = files.length > 0 && files.every((f) => selected.has(f.id));
        cb.title = "Select all files in this folder";
        cb.addEventListener("change", () => onToggleAll(cb.checked));
        th.appendChild(cb);
        trh.appendChild(th);
      }
      trh.appendChild(thEl("Name"));
      if (isTrash) trh.appendChild(thEl("Deleted"));
      else {
        trh.appendChild(thEl("Size"));
        trh.appendChild(thEl("RAG Status"));
        trh.appendChild(thEl("Query Repo"));
      }
      trh.appendChild(thEl("Updated"));
      thead.appendChild(trh);
      table.appendChild(thead);

      const tbody = document.createElement("tbody");
      for (const f of files) {
        const tr = document.createElement("tr");
        if (selected.has(f.id)) tr.className = "cdt-row-selected";
        tr.title = editMode ? "" : isTrash ? "" : "Click to open";
        tr.addEventListener("click", () => onOpenEntry(f));
        if (editMode) {
          const td = document.createElement("td");
          td.addEventListener("click", (e) => e.stopPropagation());
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = selected.has(f.id);
          cb.addEventListener("change", () => onToggleOne(f.id));
          td.appendChild(cb);
          tr.appendChild(td);
        }
        const nameTd = tdEl([span("cdt-file-icon", "📄"), document.createTextNode(f.name)]);
        nameTd.className = "cdt-name-cell";
        nameTd.title = f.name;
        tr.appendChild(nameTd);
        if (isTrash) {
          tr.appendChild(tdEl(span("muted", fmtDate(f.deleted_at))));
        } else {
          tr.appendChild(tdEl(span("muted", fmtSize(f.size))));
          const ragTd = document.createElement("td");
          const b = ragBadge ? ragBadge(f) : null;
          if (b) ragTd.appendChild(b);
          tr.appendChild(ragTd);
          const qtd = document.createElement("td");
          qtd.addEventListener("click", (e) => e.stopPropagation());
          const cell = ragCell ? ragCell(f) : null;
          if (cell) qtd.appendChild(cell);
          tr.appendChild(qtd);
        }
        tr.appendChild(tdEl(span("muted", fmtDate(f.updated_at ?? f.created_at))));
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      body.appendChild(table);
    }

    el.appendChild(body);
  }

  // Keep the fullscreen button label in sync when toggled (incl. Esc to exit).
  document.addEventListener("fullscreenchange", () => {
    const inFs = !!document.fullscreenElement;
    document.querySelectorAll(".fullscreen-btn").forEach((b) => {
      b.textContent = inFs ? "Exit Fullscreen" : "Fullscreen";
    });
  });

  // Esc closes the current document — unless we're typing in a field, fullscreen
  // (Esc is the native exit), or a modal overlay is open.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !state.path) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (document.fullscreenElement) return;
    if (document.querySelector(".overlay:not(.hidden)")) return;
    close();
  });

  // A document (file) is open in the viewer — vs. a folder listing or nothing.
  // The cloud drive uses this to avoid background refreshes clobbering an open file.
  function isOpen() {
    return state.path != null && state.kind !== "folder";
  }

  return { render, renderFolder, kindFor, localUrl, toast, close, setAttachHandler, isOpen };
})();
