// Mermaid mindmap → SVG tree diagram for note previews (mirrors the desktop renderer).
//
// Toolkit mindmaps are saved as Mermaid `mindmap` syntax (indent = nesting depth):
//   mindmap
//     root((Topic))
//       Branch
//         Detail
// Markdown would only render flat indented lines, so we rebuild the tree and lay it out
// as a tidy-tree SVG — the note preview then shows an actual tree of nodes + edges.

export function isMindmapText(text: string): boolean {
  return /^\s*mindmap\b/.test(String(text ?? ""));
}

function textWidth(label: string): number {
  const s = String(label || "");
  let w = 0;
  for (const ch of s) w += ch.charCodeAt(0) > 0x2e7f ? 13 : 7; // CJK ≈ 13px, ASCII ≈ 7px @12px font
  return w + 20;
}

function unquote(label: string): string {
  const s = String(label).trim();
  return s.length >= 2 && s.startsWith('"') && s.endsWith('"') ? s.slice(1, -1) : s;
}

interface MmNode {
  label: string;
  children: MmNode[];
  depth: number;
  x: number;
  y: number;
  _sh: number; // subtree-height cache; -1 = not computed yet
  _w: number;
}

function parseMindmap(text: string): MmNode | null {
  const nodes: Array<{ indent: number; label: string }> = [];
  for (const raw of String(text || "").split(/\r?\n/)) {
    const line = raw.replace(/%%.*$/, "").replace(/\r$/, "");
    if (!line.trim()) continue;
    const indent = line.length - line.trimStart().length;
    const token = line.trim();
    if (token === "mindmap") continue;
    if (token.startsWith("root")) {
      let inner = token.slice("root".length).replace(/^\s*\(\s*/, "").replace(/\s*\)\s*$/, "");
      while (inner.startsWith("(") && inner.endsWith(")")) inner = inner.slice(1, -1);
      nodes.push({ indent, label: unquote(inner) || "Mind Map" });
    } else {
      nodes.push({ indent, label: unquote(token) });
    }
  }
  if (!nodes.length) return null;
  const root: MmNode = { label: nodes[0].label || "Mind Map", children: [], depth: 0, x: 0, y: 0, _sh: -1, _w: 0 };
  const stack: Array<{ node: MmNode; indent: number }> = [{ node: root, indent: nodes[0].indent }];
  for (let i = 1; i < nodes.length; i++) {
    const n = nodes[i];
    while (stack.length > 1 && n.indent <= stack[stack.length - 1].indent) stack.pop();
    const child: MmNode = { label: n.label, children: [], depth: 0, x: 0, y: 0, _sh: -1, _w: 0 };
    stack[stack.length - 1].node.children.push(child);
    stack.push({ node: child, indent: n.indent });
  }
  return root;
}

// Horizontal (root-on-left) layout: branches grow rightwards, siblings stack down, so a
// dense tree stays narrow instead of spreading too wide to read. Depth → x, subtree → y.
const H_GAP = 56; // horizontal gap between a parent's right edge and a child's left edge
const V_GAP = 40; // vertical gap between siblings (≥ node height so a parent box fits)
const NODE_H = 32; // node box height

function nodeBoxW(label: string): number {
  return Math.max(textWidth(label), 48);
}

function subtreeHeight(n: MmNode): number {
  if (n._sh !== -1) return n._sh;
  if (!n.children.length) return (n._sh = NODE_H);
  const kids = n.children.reduce((s, c) => s + subtreeHeight(c), 0) + V_GAP * (n.children.length - 1);
  return (n._sh = Math.max(NODE_H, kids));
}

function layout(n: MmNode, px: number, y0: number, depth: number): void {
  n.depth = depth;
  n._w = nodeBoxW(n.label);
  n.x = px + n._w / 2;
  n.y = y0 + subtreeHeight(n) / 2;
  const nextX = px + n._w + H_GAP;
  let cy = y0;
  for (const c of n.children) {
    layout(c, nextX, cy, depth + 1);
    cy += subtreeHeight(c) + V_GAP;
  }
}

function svgEscape(s: string): string {
  const map: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
  return String(s).replace(/[&<>"]/g, (c) => map[c]);
}

export function renderMindmap(text: string): string {
  const tree = parseMindmap(text);
  if (!tree) return '<p class="cd-hint">Empty or unsupported mind map.</p>';
  layout(tree, 0, 0, 0);
  const height = Math.max(subtreeHeight(tree), 200);
  let width = 0;
  (function walk(n: MmNode) {
    width = Math.max(width, n.x + n._w / 2);
    n.children.forEach(walk);
  })(tree);
  width = Math.max(width, 320);
  const pad = 24;

  const parts = [
    `<svg class="mmd-tree" width="${Math.ceil(width + pad * 2)}" height="${Math.ceil(height + pad * 2)}" viewBox="-${pad} -${pad} ${width + pad * 2} ${height + pad * 2}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="mind map">`,
  ];
  const edges: string[] = [];
  (function walk(n: MmNode) {
    for (const c of n.children) {
      const sx = n.x + n._w / 2;
      const ex = c.x - c._w / 2;
      edges.push(`M ${sx} ${n.y} C ${sx + H_GAP / 2} ${n.y}, ${ex - H_GAP / 2} ${c.y}, ${ex} ${c.y}`);
    }
    n.children.forEach(walk);
  })(tree);
  parts.push(`<g fill="none" stroke="#a8bce0" stroke-width="1.5"><path d="${edges.join(" ")}"/></g>`);

  (function walk(n: MmNode) {
    if (n.depth === 0) {
      parts.push(
        `<rect x="${n.x - n._w / 2}" y="${n.y - NODE_H / 2}" width="${n._w}" height="${NODE_H}" rx="10" fill="#4f8cff" stroke="#3a6fd0" stroke-width="1.5"/>`,
        `<text x="${n.x}" y="${n.y}" fill="#fff" font-size="13" font-weight="700" text-anchor="middle" dominant-baseline="central">${svgEscape(n.label)}</text>`
      );
    } else {
      parts.push(
        `<rect x="${n.x - n._w / 2}" y="${n.y - NODE_H / 2}" width="${n._w}" height="${NODE_H}" rx="10" fill="#ffffff" stroke="#c8d6f0" stroke-width="1"/>`,
        `<text x="${n.x}" y="${n.y}" fill="#33415f" font-size="12" text-anchor="middle" dominant-baseline="central">${svgEscape(n.label)}</text>`
      );
    }
    n.children.forEach(walk);
  })(tree);

  parts.push("</svg>");
  return parts.join("\n");
}
