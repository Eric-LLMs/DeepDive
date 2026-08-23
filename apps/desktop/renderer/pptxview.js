// PptxViewer: render .pptx (a ZIP of DrawingML XML) as an in-window HTML slide deck,
// using the vendored JSZip (window.JSZip). Text and images are laid out at absolute
// positions converted from the slide's EMU coordinates (96 dpi). Group transforms,
// tables, and charts are approximated/omitted — this is a preview, not pixel-perfect.
const PptxViewer = (() => {
  const EMU_PER_PX = 9525; // EMU per CSS px at 96 dpi (914400 / 96)

  // Re-fit the current slide when the window resizes. Rebound per render so repeated
  // opens don't accumulate listeners.
  let onResize = null;

  function px(emu) {
    return emu / EMU_PER_PX;
  }

  // Best-effort text of an element, preserving <a:br> line breaks and <a:tab>.
  function textOf(el) {
    let out = "";
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === 3) { out += child.nodeValue; continue; }
        if (!child.tagName) continue;
        const tag = child.tagName;
        if (tag === "a:br") { out += "\n"; continue; }
        if (tag === "a:tab") { out += "\t"; continue; }
        walk(child);
      }
    };
    walk(el);
    return out;
  }

  // Inline formatting pulled from an <a:rPr> run-properties element.
  function runProps(rPr) {
    const p = { bold: false, italic: false, size: null, color: null };
    if (!rPr) return p;
    p.bold = rPr.getAttribute("b") === "1";
    p.italic = rPr.getAttribute("i") === "1";
    const sz = rPr.getAttribute("sz"); // hundredths of a point
    if (sz) p.size = Number(sz) / 100;
    const fill = rPr.getElementsByTagName("a:solidFill")[0];
    if (fill) {
      const c = fill.getElementsByTagName("a:srgbClr")[0];
      if (c) p.color = c.getAttribute("val");
    }
    return p;
  }

  // Build the HTML body of a shape's <p:txBody>: one block per paragraph, styled spans
  // per run. Font sizes are already in CSS px at 96 dpi.
  function buildTextBody(txBody) {
    const frag = document.createDocumentFragment();
    const paras = txBody.getElementsByTagName("a:p");
    for (let i = 0; i < paras.length; i++) {
      const p = paras[i];
      const div = document.createElement("div");
      div.className = "pptx-para";
      const pPr = p.getElementsByTagName("a:pPr")[0];
      if (pPr) {
        const algn = pPr.getAttribute("algn");
        if (algn === "ctr") div.style.textAlign = "center";
        else if (algn === "r") div.style.textAlign = "right";
      }
      for (const child of p.childNodes) {
        if (child.nodeType === 3) { div.append(document.createTextNode(child.nodeValue)); continue; }
        if (!child.tagName) continue;
        const tag = child.tagName;
        if (tag === "a:br") { div.append(document.createElement("br")); continue; }
        if (tag === "a:tab") { div.append(document.createTextNode("\t")); continue; }
        if (tag === "a:r" || tag === "a:fld") {
          const t = textOf(child);
          if (!t) continue;
          const span = document.createElement("span");
          span.textContent = t;
          const st = runProps(child.getElementsByTagName("a:rPr")[0]);
          if (st.bold) span.style.fontWeight = "bold";
          if (st.italic) span.style.fontStyle = "italic";
          if (st.size) span.style.fontSize = `${(st.size * 96) / 72}px`;
          if (st.color) span.style.color = "#" + st.color;
          div.append(span);
        }
      }
      frag.append(div);
    }
    return frag;
  }

  // Read a shape's <a:xfrm> position/size into CSS px at 96 dpi.
  function xfrmBox(xfrm) {
    const box = { left: 0, top: 0, width: null, height: null };
    if (!xfrm) return box;
    const off = xfrm.getElementsByTagName("a:off")[0];
    const ext = xfrm.getElementsByTagName("a:ext")[0];
    if (off) {
      box.left = px(Number(off.getAttribute("x")) || 0);
      box.top = px(Number(off.getAttribute("y")) || 0);
    }
    if (ext) {
      box.width = px(Number(ext.getAttribute("cx")) || 0);
      box.height = px(Number(ext.getAttribute("cy")) || 0);
    }
    return box;
  }

  const MIME = {
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
    gif: "image/gif", bmp: "image/bmp", svg: "image/svg+xml",
  };

  // Resolve a relationship target (e.g. "../media/image1.png") against a slide path.
  function resolveRel(slidePath, target) {
    const parts = slidePath.split("/").slice(0, -1);
    for (const seg of String(target).split("/")) {
      if (seg === "..") parts.pop();
      else if (seg && seg !== ".") parts.push(seg);
    }
    return parts.join("/");
  }

  async function slideElement(zip, slideEntry) {
    const slide = document.createElement("div");
    slide.className = "pptx-slide";
    const doc = new DOMParser().parseFromString(await slideEntry.async("string"), "application/xml");
    const tree = doc.getElementsByTagName("p:spTree")[0];
    if (!tree) return slide;

    // rId → media target for this slide's images. slideEntry.name is already
    // "ppt/slides/slideN.xml", so the rels file is its sibling under _rels/.
    const relsPath = slideEntry.name.replace(/slide(\d+)\.xml$/, "_rels/slide$1.xml.rels");
    let relsXml = null;
    const relsFile = zip.file(relsPath);
    if (relsFile) {
      const rd = new DOMParser().parseFromString(await relsFile.async("string"), "application/xml");
      relsXml = {};
      for (const r of rd.getElementsByTagName("Relationship")) {
        relsXml[r.getAttribute("Id")] = r.getAttribute("Target");
      }
    }

    for (const sp of tree.getElementsByTagName("p:sp")) {
      const txBody = sp.getElementsByTagName("p:txBody")[0];
      if (!txBody) continue;
      const box = xfrmBox(sp.getElementsByTagName("a:xfrm")[0]);
      const div = document.createElement("div");
      div.className = "pptx-shape";
      div.style.left = `${box.left}px`;
      div.style.top = `${box.top}px`;
      if (box.width != null) div.style.width = `${box.width}px`;
      if (box.height != null) div.style.height = `${box.height}px`;
      div.append(buildTextBody(txBody));
      slide.append(div);
    }

    for (const pic of tree.getElementsByTagName("p:pic")) {
      const blip = pic.getElementsByTagName("a:blip")[0];
      const embedId = blip && blip.getAttribute("r:embed");
      const target = relsXml && embedId ? relsXml[embedId] : null;
      if (!target) continue;
      const mediaPath = resolveRel(slideEntry.name, target);
      const media = zip.file(mediaPath);
      if (!media) continue;
      const b64 = await media.async("base64");
      const ext = (mediaPath.split(".").pop() || "").toLowerCase();
      const img = document.createElement("img");
      img.className = "pptx-img";
      img.src = `data:${MIME[ext] || "application/octet-stream"};base64,${b64}`;
      const box = xfrmBox(pic.getElementsByTagName("a:xfrm")[0]);
      img.style.left = `${box.left}px`;
      img.style.top = `${box.top}px`;
      img.style.width = `${box.width || 0}px`;
      img.style.height = `${box.height || 0}px`;
      slide.append(img);
    }

    return slide;
  }

  // Render a .pptx (Uint8Array/ArrayBuffer) into hostEl. hostEl is cleared and becomes
  // a slide deck with prev/next navigation. Throws on malformed input.
  async function render(bytes, hostEl) {
    if (typeof JSZip === "undefined") throw new Error("JSZip not loaded.");
    hostEl.classList.add("pptx-viewer");
    hostEl.innerHTML = "";

    const zip = await JSZip.loadAsync(bytes);
    const presFile = zip.file("ppt/presentation.xml");
    if (!presFile) throw new Error("Not a PowerPoint file (missing ppt/presentation.xml).");
    const pdoc = new DOMParser().parseFromString(await presFile.async("string"), "application/xml");
    const sldSz = pdoc.getElementsByTagName("p:sldSz")[0];
    const cx = sldSz ? px(Number(sldSz.getAttribute("cx"))) : 1280;
    const cy = sldSz ? px(Number(sldSz.getAttribute("cy"))) : 720;

    const slidePaths = Object.keys(zip.files)
      .filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n))
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    if (!slidePaths.length) throw new Error("No slides found in this PowerPoint.");

    const slides = [];
    for (const p of slidePaths) slides.push(await slideElement(zip, zip.files[p]));

    // Navigation UI.
    const nav = document.createElement("div");
    nav.className = "pptx-nav";
    const prev = document.createElement("button");
    prev.textContent = "‹ Prev";
    const label = document.createElement("span");
    label.className = "pptx-nav-label";
    const next = document.createElement("button");
    next.textContent = "Next ›";
    nav.append(prev, label, next);

    const stage = document.createElement("div");
    stage.className = "pptx-stage";
    hostEl.append(nav, stage);

    let current = 0;
    function show(i) {
      current = Math.max(0, Math.min(slides.length - 1, i));
      stage.innerHTML = "";
      const k = (stage.clientWidth || cx) / cx;
      const el = slides[current];
      // Size the slide to its native EMU dimensions so its white background paints
      // behind the absolutely-positioned shapes; scale(k) then fits it to the stage.
      el.style.width = `${cx}px`;
      el.style.height = `${cy}px`;
      el.style.transform = `scale(${k})`;
      el.style.transformOrigin = "top left";
      // Center the scaled slide in the stage when there's spare room.
      el.style.left = `${Math.max(0, (stage.clientWidth - cx * k) / 2)}px`;
      el.style.top = `${Math.max(0, (stage.clientHeight - cy * k) / 2)}px`;
      stage.style.height = `${cy * k}px`;
      stage.append(el);
      label.textContent = `Slide ${current + 1} / ${slides.length}`;
      prev.disabled = current === 0;
      next.disabled = current === slides.length - 1;
    }
    prev.onclick = () => show(current - 1);
    next.onclick = () => show(current + 1);
    onResize = () => show(current);
    window.removeEventListener("resize", onResize);
    window.addEventListener("resize", onResize);
    show(0);
  }

  return { render };
})();
