// Viewer: dispatch a file to the right in-window renderer by extension, falling back
// to the OS default app for formats the window can't render natively.
const Viewer = (() => {
  const state = { path: null, name: null, kind: null, openPath: null, zoom: 1 };

  const VIDEO_EXT = new Set(["mp4", "webm", "mov", "m4v"]);
  const AUDIO_EXT = new Set(["mp3", "wav", "m4a", "flac", "ogg"]);
  const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"]);
  const TEXT_EXT = new Set([
    "txt", "md", "json", "csv", "log", "yaml", "yml", "toml", "ini", "conf",
    "py", "js", "ts", "jsx", "tsx", "html", "css", "sh", "sql", "java", "c",
    "cpp", "h", "rs", "go", "xml",
  ]);
  const SLIDES_EXT = new Set(["ppt", "pptx"]);
  const OFFICE_EXT = new Set(["docx", "xlsx", "doc", "xls"]);

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
    const i = name.lastIndexOf(".");
    return i < 0 ? "" : name.slice(i + 1).toLowerCase();
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
    if (SLIDES_EXT.has(ext)) return "slides";
    if (OFFICE_EXT.has(ext)) return "office";
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

    const openBtn = document.createElement("button");
    openBtn.textContent = "Open in OS";
    openBtn.onclick = () => window.desktopAPI.openExternal(filePath);
    bar.appendChild(openBtn);
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

  // PowerPoint: convert to PDF via the main process (LibreOffice), then show it with
  // the PDF renderer. state.openPath keeps the original file for "Open in OS" and the
  // kind is switched to "pdf" so zoom re-renders without re-converting.
  async function renderSlides(filePath, name) {
    const el = clear();
    const status = document.createElement("div");
    status.className = "fallback";
    status.textContent = "Converting to PDF… (slower the first time)";
    el.appendChild(status);

    const res = await window.desktopAPI.convertSlides(filePath);
    if (!res.ok) {
      renderFallback(filePath, name, `Conversion failed: ${res.error}`);
      return;
    }
    state.path = res.pdfPath;
    state.kind = "pdf";
    dispatch("pdf", res.pdfPath, name);
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
      case "slides": return renderSlides(filePath, name);
      case "text": return renderText(filePath, name);
      case "office": return renderFallback(filePath, name, "Open Office documents in the system app.");
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
    dispatch(state.kind, state.path, state.name);
  }

  // Keep the fullscreen button label in sync when toggled (incl. Esc to exit).
  document.addEventListener("fullscreenchange", () => {
    const inFs = !!document.fullscreenElement;
    document.querySelectorAll(".fullscreen-btn").forEach((b) => {
      b.textContent = inFs ? "Exit Fullscreen" : "Fullscreen";
    });
  });

  return { render, kindFor, localUrl, toast };
})();
