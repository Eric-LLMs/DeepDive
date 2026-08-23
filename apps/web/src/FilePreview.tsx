// FilePreview: in-window previews of Office documents in the Cloud Drive console,
// mirroring the desktop viewer. Uses the same pure-JS renderers — SheetJS (npm `xlsx`)
// for spreadsheets/CSV/TSV, and the vendored mammoth + JSZip globals (loaded as classic
// scripts in index.html) for .docx and .pptx. Only the "⬇ Download" / "↗ Open in new tab"
// buttons fetch bytes for the user — clicking a file never downloads it.
import { useCallback, useEffect, useRef, useState } from "react";
import * as XLSX from "xlsx";
import { api } from "./api";
import type { DriveFile } from "./types";

// The vendored mammoth/JSZip bundles set these globals (index.html loads them before the
// module script). They are optional — a missing script surfaces a clear error.
declare global {
  interface Window {
    mammoth?: {
      convertToHtml(args: { arrayBuffer: ArrayBuffer; includeRawHtml?: boolean }): Promise<{ value: string }>;
    };
    JSZip?: {
      loadAsync(data: ArrayBuffer): Promise<any>;
    };
  }
}

export type OfficeKind = "docx" | "doc" | "sheet" | "pptx";

const PPTX_EXT = new Set(["pptx", "ppsx", "potx", "pptm", "ppsm", "potm"]);

// Classify a file name into a previewable Office format, or null. A Windows copy may leave
// a collision suffix after the extension ("notes.docx (1)") — strip it before matching.
export function officeKindOf(name: string): OfficeKind | null {
  const clean = String(name).replace(/\s*\(\d+\)\s*$/, "");
  const m = /\.([a-z0-9]+)$/i.exec(clean);
  const ext = m ? m[1].toLowerCase() : "";
  if (ext === "docx") return "docx";
  if (ext === "doc") return "doc";
  if (ext === "xlsx" || ext === "xls" || ext === "csv" || ext === "tsv") return "sheet";
  if (PPTX_EXT.has(ext)) return "pptx";
  return null;
}

// Strip anything dangerous from HTML produced by mammoth (a docx can embed links and raw
// HTML). Blocks script/iframe/object/etc., drops on* handlers, and allows only safe link
// schemes (data: URLs are kept for images mammoth inlines, blocked otherwise).
function sanitizeHtml(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const FORBIDDEN = new Set(["script", "iframe", "object", "embed", "base", "link", "meta", "form", "style"]);
  doc.querySelectorAll("*").forEach((n) => {
    if (FORBIDDEN.has(n.tagName.toLowerCase())) {
      n.remove();
      return;
    }
    [...n.attributes].forEach((a) => {
      const name = a.name.toLowerCase();
      if (name.startsWith("on")) {
        n.removeAttribute(a.name);
        return;
      }
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

// ── Shared loading / fallback UI ─────────────────────────────────────────────
function PreviewFallback({
  message,
  onDownload,
  onOpen,
}: {
  message: string;
  onDownload: () => void;
  onOpen: () => void;
}) {
  return (
    <div className="preview-fallback">
      <p className="preview-fallback-msg">{message}</p>
      <div className="row" style={{ gap: 8 }}>
        <button className="primary" onClick={onDownload}>⬇ Download</button>
        <button className="ghost" onClick={onOpen}>↗ Open in new tab</button>
      </div>
    </div>
  );
}

// ── Word (.docx) ─────────────────────────────────────────────────────────────
function DocxPreview({ bytes, onError }: { bytes: ArrayBuffer; onError: (m: string) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const mammoth = window.mammoth;
        if (!mammoth) throw new Error("mammoth not loaded.");
        const result = await mammoth.convertToHtml({ arrayBuffer: bytes, includeRawHtml: false });
        if (alive && ref.current) ref.current.innerHTML = sanitizeHtml(result.value);
      } catch (e) {
        if (alive) onError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [bytes, onError]);
  return <div className="docx-viewer" ref={ref} />;
}

// ── Excel / CSV / TSV ────────────────────────────────────────────────────────
function isDelimited(name: string): boolean {
  const m = /\.([a-z0-9]+)$/i.exec(String(name).replace(/\s*\(\d+\)\s*$/, ""));
  const ext = m ? m[1].toLowerCase() : "";
  return ext === "csv" || ext === "tsv";
}

function SheetPreview({ bytes, name, onError }: { bytes: ArrayBuffer; name: string; onError: (m: string) => void }) {
  const [active, setActive] = useState(0);
  const [sheets, setSheets] = useState<string[]>([]);
  const [htmls, setHtmls] = useState<string[]>([]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        // CSV/TSV are decoded to a string first so SheetJS auto-detects the delimiter
        // (comma / tab) instead of treating the bytes as a zip.
        const wb = isDelimited(name)
          ? XLSX.read(new TextDecoder("utf-8").decode(bytes), { type: "string" })
          : XLSX.read(new Uint8Array(bytes), { type: "array" });
        if (!wb.SheetNames.length) throw new Error("No sheets in workbook.");
        const names = wb.SheetNames;
        const html = names.map((sn) => XLSX.utils.sheet_to_html(wb.Sheets[sn]));
        if (alive) {
          setSheets(names);
          setHtmls(html);
        }
      } catch (e) {
        if (alive) onError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [bytes, name, onError]);
  if (!htmls.length) return <div className="sheet-viewer" />;
  return (
    <div className="sheet-viewer">
      <div className="sheet-tabs">
        {sheets.map((s, i) => (
          <button key={s} className={i === active ? "active" : ""} onClick={() => setActive(i)}>
            {s}
          </button>
        ))}
      </div>
      <div className="sheet-panels" dangerouslySetInnerHTML={{ __html: htmls[active] }} />
    </div>
  );
}

// ── PowerPoint (.pptx) ───────────────────────────────────────────────────────
// Ported from apps/desktop/renderer/pptxview.js — a small DrawingML parser that reads
// ppt/slides/slideN.xml, lays out text shapes and p:pic images at absolute EMU-derived
// positions (96 dpi), and renders a prev/next slide deck with the slide scaled to fit.
const EMU_PER_PX = 9525; // EMU per CSS px at 96 dpi (914400 / 96)

function px(emu: number): number {
  return emu / EMU_PER_PX;
}

// Best-effort text of an element, preserving <a:br> line breaks and <a:tab>.
function textOf(el: Element): string {
  let out = "";
  const walk = (node: Node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        out += child.nodeValue;
        continue;
      }
      if (!(child instanceof Element)) continue;
      const tag = child.tagName;
      if (tag === "a:br") {
        out += "\n";
        continue;
      }
      if (tag === "a:tab") {
        out += "\t";
        continue;
      }
      walk(child);
    }
  };
  walk(el);
  return out;
}

// Inline formatting pulled from an <a:rPr> run-properties element.
function runProps(rPr: Element | null): { bold: boolean; italic: boolean; size: number | null; color: string | null } {
  const p = { bold: false, italic: false, size: null, color: null } as { bold: boolean; italic: boolean; size: number | null; color: string | null };
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

// Build the HTML body of a shape's <p:txBody>: one block per paragraph, styled spans per run.
function buildTextBody(txBody: Element): DocumentFragment {
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
      if (child.nodeType === 3) {
        div.append(document.createTextNode(child.nodeValue ?? ""));
        continue;
      }
      if (!(child instanceof Element)) continue;
      const tag = child.tagName;
      if (tag === "a:br") {
        div.append(document.createElement("br"));
        continue;
      }
      if (tag === "a:tab") {
        div.append(document.createTextNode("\t"));
        continue;
      }
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
function xfrmBox(xfrm: Element | null): { left: number; top: number; width: number | null; height: number | null } {
  const box = { left: 0, top: 0, width: null as number | null, height: null as number | null };
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

const MIME: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  bmp: "image/bmp",
  svg: "image/svg+xml",
};

// Resolve a relationship target (e.g. "../media/image1.png") against a slide path.
function resolveRel(slidePath: string, target: string): string {
  const parts = slidePath.split("/").slice(0, -1);
  for (const seg of String(target).split("/")) {
    if (seg === "..") parts.pop();
    else if (seg && seg !== ".") parts.push(seg);
  }
  return parts.join("/");
}

async function slideElement(zip: any, slideEntry: any): Promise<HTMLElement> {
  const slide = document.createElement("div");
  slide.className = "pptx-slide";
  const doc = new DOMParser().parseFromString(await slideEntry.async("string"), "application/xml");
  const tree = doc.getElementsByTagName("p:spTree")[0];
  if (!tree) return slide;

  // rId → media target for this slide's images. slideEntry.name is already
  // "ppt/slides/slideN.xml", so the rels file is its sibling under _rels/.
  const relsPath = slideEntry.name.replace(/slide(\d+)\.xml$/, "_rels/slide$1.xml.rels");
  let relsXml: Record<string, string> | null = null;
  const relsFile = zip.file(relsPath);
  if (relsFile) {
    const rd = new DOMParser().parseFromString(await relsFile.async("string"), "application/xml");
    relsXml = {};
    for (const r of rd.getElementsByTagName("Relationship")) {
      relsXml[r.getAttribute("Id") ?? ""] = r.getAttribute("Target") ?? "";
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

function PptxPreview({ bytes, onError }: { bytes: ArrayBuffer; onError: (m: string) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const hostEl = ref.current;
    if (!hostEl) return;
    // Clear any residue from a previous effect run (React StrictMode re-runs effects in
    // dev, and the async JSZip parse may resolve after the dead run's cleanup already ran).
    hostEl.innerHTML = "";
    let alive = true;
    let cleanup: () => void = () => {};
    (async () => {
      try {
        const JSZip = window.JSZip;
        if (!JSZip) throw new Error("JSZip not loaded.");
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

        const slides: HTMLElement[] = [];
        for (const p of slidePaths) slides.push(await slideElement(zip, zip.files[p]));
        // The component may have unmounted (or been re-mounted) while parsing — don't
        // touch the DOM for a dead run, or the deck would be appended twice.
        if (!alive) return;

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
        const show = (i: number) => {
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
        };
        prev.onclick = () => show(current - 1);
        next.onclick = () => show(current + 1);
        const onResize = () => show(current);
        window.addEventListener("resize", onResize);
        cleanup = () => window.removeEventListener("resize", onResize);
        show(0);
      } catch (e) {
        if (alive) onError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
      cleanup();
    };
  }, [bytes, onError]);
  return <div className="pptx-wrap" ref={ref} />;
}

// ── Main preview panel ───────────────────────────────────────────────────────
export default function FilePreview({
  file,
  onClose,
  onDownload,
  onOpen,
}: {
  file: DriveFile;
  onClose: () => void;
  onDownload: () => void;
  onOpen: () => void;
}) {
  const kind = officeKindOf(file.name);
  const [bytes, setBytes] = useState<ArrayBuffer | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const onError = useCallback((m: string) => setError(m), []);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const blob = await api.downloadFile(file.id);
        const buf = await blob.arrayBuffer();
        if (buf.byteLength === 0) {
          throw new Error("This file is empty (0 bytes) — it may have failed to upload.");
        }
        if (alive) setBytes(buf);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [file.id]);

  return (
    <div className="note-editor file-preview">
      <div className="note-editor-toolbar">
        <span className="note-editor-title" title={file.folder_path ?? ""}>
          📄 {file.name}
        </span>
        <span style={{ flex: 1 }} />
        <button className="ghost" onClick={onDownload} title="Download this file">⬇ Download</button>
        <button className="ghost" onClick={onClose} title="Close preview">✖</button>
      </div>
      {error ? (
        <PreviewFallback message={error} onDownload={onDownload} onOpen={onOpen} />
      ) : loading || !bytes ? (
        <div className="preview-status">Loading…</div>
      ) : kind === "docx" ? (
        <DocxPreview bytes={bytes} onError={onError} />
      ) : kind === "sheet" ? (
        <SheetPreview bytes={bytes} name={file.name} onError={onError} />
      ) : kind === "pptx" ? (
        <PptxPreview bytes={bytes} onError={onError} />
      ) : (
        <PreviewFallback
          message="This format can't be previewed in the browser. Use the buttons below to download it or open it in a new tab."
          onDownload={onDownload}
          onOpen={onOpen}
        />
      )}
    </div>
  );
}
