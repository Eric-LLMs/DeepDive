// Cloud Drive page: left tree (My Drive + workspace folders + subfolders + Trash),
// right file list. Folders are first-class: created in any workspace / My Drive,
// files moved across scopes. Deleting a file (or a whole folder) sends it to the
// trash where it can be restored or permanently purged. A search box with scope
// filtering + fuzzy autocomplete jumps straight to a file's folder.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { renderMarkdown } from "./markdown";
import FilePreview, { officeKindOf } from "./FilePreview";
import type {
  DriveFile,
  DriveFolder,
  Me,
  ShareEntry,
  Workspace,
  WorkspaceActivity,
  WorkspaceMember,
  WorkspaceUser,
} from "./types";

const MAX_BROWSER_UPLOAD = 256 * 1024 * 1024; // bytes; larger files go through the desktop client
const REFRESH_MS = 5000;

const RAG_WORKING = new Set(["PENDING", "PARSING", "CHUNKING", "EMBEDDING"]);
const RAG_LABEL: Record<string, string> = {
  PENDING: "Pending",
  PARSING: "Parsing",
  CHUNKING: "Chunking",
  EMBEDDING: "Embedding",
  INDEXED: "Indexed",
  FAILED: "Failed",
};

// Extensions the query repository can index (text / subtitle / PDF / Word). Everything
// else (audio, video, legacy formats) shows a disabled "暂不支持" button.
const RAG_IMPORTABLE_EXTS = new Set([
  ".txt", ".md", ".markdown", ".text", ".log", ".json", ".csv",
  ".srt", ".vtt", ".lrc", ".pdf", ".docx",
]);

function fileExt(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function fmtDate(s: string | null): string {
  return s ? new Date(s).toLocaleString() : "—";
}

// Coarse ingest ETA for the "Processing…" countdown. Real ingest time varies wildly (PDF
// table vision, LLM contextual enrichment), so this is a floor + size-proportional term,
// clamped and clearly approximate; the phase badge + 5s auto-refresh count it down live.
function estimateIngestSeconds(size: number, name: string): number {
  const mb = Math.max(0.1, (size || 0) / (1024 * 1024));
  const perMb = name.toLowerCase().endsWith(".pdf") ? 12 : 6; // PDF table vision is slower
  return Math.min(600, Math.round(20 + mb * perMb));
}

function ingestEtaRemaining(size: number, name: string, startMs?: number): number {
  if (!startMs) return 0;
  return Math.max(0, estimateIngestSeconds(size, name) - Math.floor((Date.now() - startMs) / 1000));
}

function ingestEtaSuffix(size: number, name: string, startMs?: number): string {
  const remain = ingestEtaRemaining(size, name, startMs);
  return remain > 0 ? ` · ~${remain}s` : "";
}

function toHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Case-insensitive fuzzy score for the search autocomplete. Lower is better; null = no
// match. Ranked: exact/prefix on the name > substring on the name > folder path hit >
// loose subsequence of the name. Offsets are folded in so earlier matches win.
function fuzzyScore(hay: string, q: string): number | null {
  const h = hay.toLowerCase();
  const qq = q.toLowerCase();
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

// ── Location model ─────────────────────────────────────────────────────────────
// The current folder shown in the right pane. A workspace is a top-level folder;
// a "folder" loc is a subfolder inside a workspace (or My Drive when ws is null).
type Loc =
  | { kind: "root" }
  | { kind: "workspace"; id: string }
  | { kind: "folder"; ws: string | null; path: string }
  | { kind: "trash" };

function locKey(l: Loc): string {
  if (l.kind === "root") return "root";
  if (l.kind === "trash") return "trash";
  if (l.kind === "workspace") return `ws:${l.id}`;
  return l.ws ? `ws:${l.ws}/${l.path}` : l.path;
}

function locLabel(l: Loc, workspaces: Workspace[]): string {
  if (l.kind === "root") return "My Drive";
  if (l.kind === "trash") return "Trash";
  if (l.kind === "workspace") {
    return workspaces.find((w) => w.id === l.id)?.name ?? "Workspace";
  }
  const base = l.ws ? workspaces.find((w) => w.id === l.ws)?.name ?? "Workspace" : "My Drive";
  return `${base} / ${l.path.split("/").join(" / ")}`;
}

// ── Folder tree ────────────────────────────────────────────────────────────────
interface TreeNode {
  key: string;
  name: string;
  path: string;
  ws: string | null;
  children: TreeNode[];
}

// Build the subfolder tree for one scope (My Drive when ws is null, else a workspace).
function buildTree(paths: string[], ws: string | null): TreeNode[] {
  const root: TreeNode[] = [];
  const map = new Map<string, TreeNode>();
  const base = ws ? `ws:${ws}/` : "";
  for (const p of paths) {
    const segs = p.split("/").filter(Boolean);
    let cur = root;
    let acc = "";
    for (const s of segs) {
      acc = acc ? `${acc}/${s}` : s;
      const key = base + acc;
      let node = map.get(key);
      if (!node) {
        node = { key, name: s, path: acc, ws, children: [] };
        map.set(key, node);
        cur.push(node);
      }
      cur = node.children;
    }
  }
  return root;
}

interface TreeItem {
  key: string;
  name: string;
  icon: string;
  loc: Loc;
  children: TreeNode[];
}

function DriveTree({
  items,
  collapsed,
  selectedKey,
  onSelect,
  onToggle,
}: {
  items: TreeItem[];
  collapsed: Set<string>;
  selectedKey: string;
  onSelect: (loc: Loc) => void;
  onToggle: (key: string) => void;
}) {
  return (
    <>
      {items.map((item) => {
        const open = !collapsed.has(item.key);
        const hasKids = item.children.length > 0;
        return (
          <div key={item.key}>
            <div
              className={"drive-node" + (selectedKey === item.key ? " active" : "")}
              style={{ paddingLeft: 8 }}
              onClick={() => onSelect(item.loc)}
            >
              <span
                className="drive-caret"
                onClick={(e) => {
                  e.stopPropagation();
                  onToggle(item.key);
                }}
              >
                {hasKids ? (open ? "▾" : "▸") : ""}
              </span>
              <span className="drive-folder-icon">{item.icon}</span>
              <span className="drive-node-name">{item.name}</span>
            </div>
            {hasKids && open && (
              <SubNodes
                nodes={item.children}
                depth={1}
                collapsed={collapsed}
                selectedKey={selectedKey}
                onSelect={onSelect}
                onToggle={onToggle}
              />
            )}
          </div>
        );
      })}
    </>
  );
}

function SubNodes({
  nodes,
  depth,
  collapsed,
  selectedKey,
  onSelect,
  onToggle,
}: {
  nodes: TreeNode[];
  depth: number;
  collapsed: Set<string>;
  selectedKey: string;
  onSelect: (loc: Loc) => void;
  onToggle: (key: string) => void;
}) {
  return (
    <>
      {nodes.map((n) => {
        const open = !collapsed.has(n.key);
        const hasKids = n.children.length > 0;
        return (
          <div key={n.key}>
            <div
              className={"drive-node" + (selectedKey === n.key ? " active" : "")}
              style={{ paddingLeft: 8 + depth * 14 }}
              onClick={() => onSelect({ kind: "folder", ws: n.ws, path: n.path })}
            >
              <span
                className="drive-caret"
                onClick={(e) => {
                  e.stopPropagation();
                  onToggle(n.key);
                }}
              >
                {hasKids ? (open ? "▾" : "▸") : ""}
              </span>
              <span className="drive-folder-icon">📁</span>
              <span className="drive-node-name">{n.name}</span>
            </div>
            {hasKids && open && (
              <SubNodes
                nodes={n.children}
                depth={depth + 1}
                collapsed={collapsed}
                selectedKey={selectedKey}
                onSelect={onSelect}
                onToggle={onToggle}
              />
            )}
          </div>
        );
      })}
    </>
  );
}

// ── Dialogs ────────────────────────────────────────────────────────────────────
function ShareModal({ file, onClose }: { file: DriveFile; onClose: () => void }) {
  const [shares, setShares] = useState<ShareEntry[]>([]);
  const [publicOn, setPublicOn] = useState(false);
  const [grantee, setGrantee] = useState("");
  const [permission, setPermission] = useState("read");
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api.listShares(file.id).then((r) => setShares(r.shares)).catch((e) => setError(String(e)));
  }, [file.id]);
  useEffect(load, [load]);

  const add = async () => {
    setError("");
    setMsg("");
    try {
      await api.shareFile(file.id, {
        grantee_user_id: publicOn ? null : grantee.trim() || null,
        permission,
      });
      setGrantee("");
      setMsg(publicOn ? "Public link created." : "Share saved.");
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  const revoke = async (granteeId: string) => {
    setError("");
    try {
      await api.unshareFile(file.id, granteeId);
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 460 }}>
        <button className="modal-close ghost" onClick={onClose}>✖ Close</button>
        <h2 style={{ marginTop: 0 }}>Share</h2>
        <p className="muted" style={{ marginTop: 0 }}>{file.name}</p>

        <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
          <label className="row" style={{ gap: 4, alignItems: "center" }}>
            <input type="checkbox" checked={publicOn} onChange={(e) => setPublicOn(e.target.checked)} />
            Public link (any signed-in user)
          </label>
          {!publicOn && (
            <input
              placeholder="User UUID"
              value={grantee}
              onChange={(e) => setGrantee(e.target.value)}
              style={{ width: 200 }}
            />
          )}
          <select value={permission} onChange={(e) => setPermission(e.target.value)}>
            <option value="read">Read</option>
            <option value="write">Write</option>
          </select>
          <button className="primary" onClick={add}>Add</button>
        </div>
        {msg && <p className="success" style={{ fontSize: 13 }}>{msg}</p>}
        {error && <p className="error" style={{ fontSize: 13 }}>{error}</p>}

        {shares.length === 0 ? (
          <p className="muted">No shares yet.</p>
        ) : (
          <table className="st-table">
            <thead>
              <tr><th>Grantee</th><th>Permission</th><th></th></tr>
            </thead>
            <tbody>
              {shares.map((s) => (
                <tr key={s.grantee_user_id ?? "public"}>
                  <td>{s.grantee_user_id ?? "🌍 Public"}</td>
                  <td>{s.permission}</td>
                  <td>
                    <button className="ghost" onClick={() => revoke(s.grantee_user_id ?? "public")}>
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function NewFolderModal({
  loc,
  onClose,
  onCreated,
}: {
  loc: Loc;
  onClose: () => void;
  onCreated: (requested: string, finalName: string) => void;
}) {
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  const save = async () => {
    const requested = name.trim();
    if (!requested) {
      setError("Folder name is required.");
      return;
    }
    setError("");
    try {
      const workspace_id =
        loc.kind === "folder" ? loc.ws : loc.kind === "workspace" ? loc.id : null;
      const parent_path = loc.kind === "folder" ? loc.path : null;
      const created = await api.createFolder({ name: requested, parent_path, workspace_id });
      onCreated(requested, created.name);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 400 }}>
        <button className="modal-close ghost" onClick={onClose}>✖ Close</button>
        <h2 style={{ marginTop: 0 }}>New folder</h2>
        <p className="muted" style={{ marginTop: 0 }}>Created in: {locLabel(loc, [])}</p>
        <label className="field-label">Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()}
          autoFocus
          placeholder="e.g. English/Vocab"
        />
        {error && <p className="error" style={{ fontSize: 13 }}>{error}</p>}
        <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
          <button className="ghost" onClick={onClose}>Cancel</button>
          <button className="primary" onClick={save}>Create</button>
        </div>
      </div>
    </div>
  );
}

function NewTextModal({
  loc,
  onClose,
  onCreated,
}: {
  loc: Loc;
  onClose: () => void;
  onCreated: (requested: string, finalName: string) => void;
}) {
  const [name, setName] = useState("untitled.txt");
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const save = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("File name is required.");
      return;
    }
    setError("");
    setSaving(true);
    try {
      const workspace_id =
        loc.kind === "folder" ? loc.ws : loc.kind === "workspace" ? loc.id : null;
      const folder_path = loc.kind === "folder" ? loc.path : null;
      const bytes = new TextEncoder().encode(content);
      const hex = toHex(await crypto.subtle.digest("SHA-256", bytes));
      const finalName = /\.\w+$/.test(trimmed) ? trimmed : `${trimmed}.txt`;
      const init = await api.initUpload({
        sha256: hex,
        size: bytes.length,
        name: finalName,
        folder_path,
        mime_type: "text/plain",
        workspace_id,
      });
      if (init.status === "instant") {
        onCreated(finalName, init.asset?.name ?? finalName);
        return;
      }
      const assetId = init.asset_id!;
      const chunkSize = init.chunk_size!;
      const num = init.num_chunks!;
      for (let i = 0; i < num; i++) {
        const start = i * chunkSize;
        const slice = bytes.slice(start, Math.min(bytes.length, start + chunkSize));
        await api.uploadChunk(assetId, i, new Blob([slice]));
      }
      const done = await api.completeUpload(assetId);
      onCreated(finalName, done.asset?.name ?? finalName);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 420 }}>
        <button className="modal-close ghost" onClick={onClose}>✖ Close</button>
        <h2 style={{ marginTop: 0 }}>New text file</h2>
        <p className="muted" style={{ marginTop: 0 }}>Created in: {locLabel(loc, [])}</p>
        <label className="field-label">File name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !saving && save()}
          autoFocus
        />
        <label className="field-label" style={{ marginTop: 10 }}>Content (optional)</label>
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={6}
          style={{ width: "100%", resize: "vertical" }}
          placeholder="Type text to save in the file…"
        />
        {error && <p className="error" style={{ fontSize: 13 }}>{error}</p>}
        <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
          <button className="ghost" onClick={onClose}>Cancel</button>
          <button className="primary" onClick={save} disabled={saving}>
            {saving ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

function MoveModal({
  files,
  workspaces,
  defaultWs,
  defaultPath,
  onClose,
  onMoved,
}: {
  files: DriveFile[];
  workspaces: Workspace[];
  defaultWs: string | null;
  defaultPath: string;
  onClose: () => void;
  onMoved: () => void;
}) {
  const [ws, setWs] = useState(defaultWs ?? "");
  const [path, setPath] = useState(defaultPath);
  const [error, setError] = useState("");

  const save = async () => {
    setError("");
    try {
      const workspace_id = ws ? ws : null;
      const folder_path = path.trim() ? path.trim() : null;
      for (const f of files) {
        await api.moveFile(f.id, { workspace_id, folder_path });
      }
      onMoved();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 420 }}>
        <button className="modal-close ghost" onClick={onClose}>✖ Close</button>
        <h2 style={{ marginTop: 0 }}>Move {files.length > 1 ? `${files.length} files` : "file"}</h2>
        <label className="field-label">Destination workspace</label>
        <select value={ws} onChange={(e) => setWs(e.target.value)}>
          <option value="">My Drive</option>
          {workspaces.map((w) => (
            <option key={w.id} value={w.id}>{w.name}</option>
          ))}
        </select>
        <label className="field-label">Folder path (within workspace; empty = root)</label>
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="e.g. English/Vocab"
        />
        {error && <p className="error" style={{ fontSize: 13 }}>{error}</p>}
        <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
          <button className="ghost" onClick={onClose}>Cancel</button>
          <button className="primary" onClick={save}>Move</button>
        </div>
      </div>
    </div>
  );
}

function RenameModal({
  file,
  onClose,
  onSaved,
}: {
  file: DriveFile;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(file.name);
  const [folder, setFolder] = useState(file.folder_path ?? "");
  const [error, setError] = useState("");

  const save = async () => {
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    setError("");
    try {
      await api.renameFile(file.id, {
        name: name.trim(),
        folder_path: folder.trim() || null,
      });
      onSaved();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 400 }}>
        <button className="modal-close ghost" onClick={onClose}>✖ Close</button>
        <h2 style={{ marginTop: 0 }}>Rename</h2>
        <label className="field-label">Name</label>
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        <label className="field-label">Folder path (e.g. &quot;Course/English&quot;)</label>
        <input value={folder} onChange={(e) => setFolder(e.target.value)} />
        {error && <p className="error" style={{ fontSize: 13 }}>{error}</p>}
        <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
          <button className="ghost" onClick={onClose}>Cancel</button>
          <button className="primary" onClick={save}>Save</button>
        </div>
      </div>
    </div>
  );
}

const ROLE_LABEL: Record<string, string> = {
  owner: "Owner", admin: "Admin", editor: "Editor", viewer: "Viewer",
};

const ACTIVITY_PAGE = 20;

// Human labels for audit-trail action codes (mirrors the action strings logged by
// core.application.drive_service). Unknown codes fall back to the raw action.
const ACTION_LABEL: Record<string, string> = {
  "file.create": "Uploaded file",
  "file.rename": "Renamed file",
  "file.move": "Moved file",
  "file.delete": "Moved file to trash",
  "file.restore": "Restored file",
  "file.purge": "Permanently deleted file",
  "trash.empty": "Emptied trash",
  "file.share": "Shared file",
  "file.unshare": "Revoked share",
  "folder.create": "Created folder",
  "folder.rename": "Renamed folder",
  "folder.delete": "Deleted folder",
  "workspace.create": "Created workspace",
  "workspace.rename": "Renamed workspace",
  "workspace.delete": "Deleted workspace",
  "member.add": "Added member",
  "member.update": "Changed member role",
  "member.remove": "Removed member",
};

// Workspace management: member list (owner row + member rows with role / remove),
// add-by-user-id, rename, and delete. Mutating controls are owner-only; the owner is
// not a workspace_members row, so their row is rendered from workspace.owner_id.
function WorkspaceManageModal({
  ws,
  me,
  onClose,
  onChanged,
}: {
  ws: Workspace;
  me: Me | null;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}) {
  const [tab, setTab] = useState<"members" | "logs">("members");

  // Members tab state.
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [userText, setUserText] = useState("");
  const [userSug, setUserSug] = useState<WorkspaceUser[]>([]);
  const [selUser, setSelUser] = useState<WorkspaceUser | null>(null);
  const [newRole, setNewRole] = useState("viewer");
  const [renameName, setRenameName] = useState(ws.name);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Activity-log tab state. Search fields are draft inputs; the applied values (which
  // the query uses) only update when the user hits Search, so typing doesn't spam API.
  const [logQInput, setLogQInput] = useState("");
  const [logStartInput, setLogStartInput] = useState("");
  const [logEndInput, setLogEndInput] = useState("");
  const [logQ, setLogQ] = useState("");
  const [logStart, setLogStart] = useState("");
  const [logEnd, setLogEnd] = useState("");
  const [logs, setLogs] = useState<WorkspaceActivity[]>([]);
  const [logTotal, setLogTotal] = useState(0);
  const [logOffset, setLogOffset] = useState(0);
  const [logErr, setLogErr] = useState("");

  const isOwner = me != null && me.user_id === ws.owner_id;
  // Admins (like the owner) manage members and see the logs; only the owner can
  // rename or delete the workspace itself.
  const canManageMembers = isOwner || ws.role === "admin";

  const load = useCallback(() => {
    setError("");
    api.listWorkspaceMembers(ws.id).then((r) => setMembers(r.members)).catch((e) => setError(String(e)));
  }, [ws.id]);
  useEffect(load, [load]);

  const loadLogs = useCallback(() => {
    setLogErr("");
    api.listWorkspaceActivity(ws.id, {
      q: logQ || undefined,
      start: logStart || undefined,
      end: logEnd || undefined,
      limit: ACTIVITY_PAGE,
      offset: logOffset,
    }).then((r) => { setLogs(r.items); setLogTotal(r.total); })
      .catch((e) => setLogErr(String(e)));
  }, [ws.id, logQ, logStart, logEnd, logOffset]);
  useEffect(() => { if (tab === "logs") loadLogs(); }, [tab, loadLogs]);

  const applyLogFilters = () => {
    setLogQ(logQInput.trim());
    setLogStart(logStartInput);
    setLogEnd(logEndInput);
    setLogOffset(0);
  };

  const onUserText = (v: string) => {
    setUserText(v);
    setSelUser(null);
    if (!v.trim()) { setUserSug([]); return; }
    api.searchUsers(v, 8).then((r) => setUserSug(r.users)).catch(() => setUserSug([]));
  };

  const selectUser = (u: WorkspaceUser) => {
    setSelUser(u);
    setUserText(`${u.username} · ${u.user_id.slice(0, 8)}…`);
    setUserSug([]);
  };

  const add = async () => {
    setError("");
    setMsg("");
    if (!selUser) {
      setError("Pick a matching user from the suggestions, then Add.");
      return;
    }
    try {
      await api.addWorkspaceMember(ws.id, { user_id: selUser.user_id, role: newRole });
      setSelUser(null);
      setUserText("");
      setMsg(`Added ${selUser.username} as ${newRole}.`);
      load();
      onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  const updateRole = async (m: WorkspaceMember, role: string) => {
    setError("");
    setMsg("");
    try {
      await api.updateWorkspaceMember(ws.id, m.user_id, role);
      setMsg(`Role updated for ${m.display_name ?? m.username ?? m.user_id}.`);
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  const remove = async (m: WorkspaceMember) => {
    setError("");
    setMsg("");
    try {
      await api.removeWorkspaceMember(ws.id, m.user_id);
      setMsg(`Removed ${m.display_name ?? m.username ?? m.user_id}.`);
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  const saveRename = async () => {
    if (!renameName.trim()) {
      setError("Name is required.");
      return;
    }
    setError("");
    setMsg("");
    try {
      await api.renameWorkspace(ws.id, renameName.trim());
      setMsg("Workspace renamed.");
      onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  const doDelete = async () => {
    setError("");
    try {
      await api.deleteWorkspace(ws.id);
      onClose();
      onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 680 }}>
        <button className="modal-close ghost" onClick={onClose}>✖ Close</button>
        <h2 style={{ marginTop: 0 }}>Manage &quot;{ws.name}&quot;</h2>

        {msg && <p className="success" style={{ fontSize: 13 }}>{msg}</p>}
        {error && <p className="error" style={{ fontSize: 13 }}>{error}</p>}

        <div className="tabs">
          <button className={tab === "members" ? "tab active" : "tab"} onClick={() => setTab("members")}>Members</button>
          <button className={tab === "logs" ? "tab active" : "tab"} onClick={() => setTab("logs")}>Activity Logs</button>
        </div>

        {tab === "members" ? (
          <>
        <h3 style={{ marginBottom: 4 }}>Members</h3>
        <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>
          {isOwner
            ? "Members share the workspace. Admins and editors change files and folders; viewers read. Only the owner can rename, delete the workspace, or assign the admin role."
            : "Admins and editors change files and folders; viewers read. Only the owner can rename/delete the workspace or assign the admin role."}
        </p>

        <div className="ws-member-row">
          <span className="ws-member-id" title={ws.owner_id}>
            {ws.owner_display_name ?? ws.owner_username ?? ws.owner_id}
          </span>
          <span className="ws-member-meta" title={ws.owner_id}>{ws.owner_id.slice(0, 8)}…</span>
          <span className="badge">Owner</span>
          <span style={{ flex: 1 }} />
          {me?.user_id === ws.owner_id && <span className="muted">you</span>}
        </div>

        {members.map((m) => (
          <div className="ws-member-row" key={m.user_id}>
            <span className="ws-member-id" title={m.user_id}>
              {m.display_name ?? m.username ?? m.user_id}
            </span>
            <span className="ws-member-meta" title={m.user_id}>{m.user_id.slice(0, 8)}…</span>
            {canManageMembers && (isOwner || m.role !== "admin") ? (
              <select value={m.role} onChange={(e) => updateRole(m, e.target.value)}>
                {isOwner && <option value="admin">Admin</option>}
                <option value="editor">Editor</option>
                <option value="viewer">Viewer</option>
              </select>
            ) : (
              <span className="badge">{ROLE_LABEL[m.role] ?? m.role}</span>
            )}
            <span style={{ flex: 1 }} />
            {me?.user_id === m.user_id && <span className="muted">you</span>}
            {(isOwner || (canManageMembers && m.role !== "admin")) && (
              <button className="ghost danger" onClick={() => remove(m)} title="Remove member">✕</button>
            )}
          </div>
        ))}

        {canManageMembers && (
          <div style={{ position: "relative", marginTop: 12 }}>
            <div className="row" style={{ gap: 8 }}>
              <input
                placeholder="Add by name or user id…"
                value={userText}
                onChange={(e) => onUserText(e.target.value)}
                onBlur={() => setTimeout(() => setUserSug([]), 120)}
                style={{ flex: 1 }}
              />
              <select value={newRole} onChange={(e) => setNewRole(e.target.value)}>
                {isOwner && <option value="admin">Admin</option>}
                <option value="editor">Editor</option>
                <option value="viewer">Viewer</option>
              </select>
              <button className="primary" onClick={add}>Add</button>
            </div>
            {userSug.length > 0 && (
              <div className="drive-suggest">
                {userSug.map((u) => (
                  <div
                    key={u.user_id}
                    className="drive-suggest-item"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => selectUser(u)}
                  >
                    <span className="drive-suggest-name">{u.display_name ?? u.username}</span>
                    <span className="drive-suggest-meta" title={u.user_id}>{u.user_id}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "16px 0" }} />

        <h3 style={{ marginBottom: 8 }}>Settings</h3>
        {isOwner ? (
          <>
            <div className="row" style={{ gap: 8 }}>
              <input
                value={renameName}
                onChange={(e) => setRenameName(e.target.value)}
                style={{ flex: 1 }}
                placeholder="Workspace name"
              />
              <button className="ghost" onClick={saveRename}>Rename</button>
            </div>
            <div className="row" style={{ gap: 8, marginTop: 12 }}>
              <button className="ghost danger" onClick={() => setConfirmDelete(true)}>Delete workspace</button>
            </div>
          </>
        ) : (
          <p className="muted" style={{ fontSize: 12 }}>
            Only the owner can rename or delete this workspace.
          </p>
        )}
          </>
        ) : (
          <>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <input
                placeholder="Search actor, file, folder, or user id…"
                value={logQInput}
                onChange={(e) => setLogQInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") applyLogFilters(); }}
                style={{ flex: 1, minWidth: 180 }}
              />
              <label className="act-filter">
                From
                <input type="date" value={logStartInput} onChange={(e) => setLogStartInput(e.target.value)} />
              </label>
              <label className="act-filter">
                To
                <input type="date" value={logEndInput} onChange={(e) => setLogEndInput(e.target.value)} />
              </label>
              <button className="primary" onClick={applyLogFilters}>Search</button>
            </div>

            <div className="row" style={{ gap: 8, marginTop: 8, fontSize: 12, alignItems: "center" }}>
              <span className="muted">
                {logTotal === 0
                  ? "No entries"
                  : `Showing ${logOffset + 1}–${Math.min(logOffset + ACTIVITY_PAGE, logTotal)} of ${logTotal}`}
              </span>
              <span style={{ flex: 1 }} />
              <button
                className="ghost"
                disabled={logOffset <= 0}
                onClick={() => setLogOffset((o) => Math.max(0, o - ACTIVITY_PAGE))}
              >‹ Prev</button>
              <button
                className="ghost"
                disabled={logOffset + ACTIVITY_PAGE >= logTotal}
                onClick={() => setLogOffset((o) => o + ACTIVITY_PAGE)}
              >Next ›</button>
            </div>

            {logErr && <p className="error" style={{ fontSize: 12 }}>{logErr}</p>}

            <div className="activity-table-wrap">
              <table className="activity-table">
                <thead>
                  <tr><th>Time</th><th>Action</th><th>Actor</th><th>Target</th><th>Detail</th></tr>
                </thead>
                <tbody>
                  {logs.map((r) => (
                    <tr key={r.id}>
                      <td className="muted" title={r.created_at ?? ""}>{fmtDate(r.created_at)}</td>
                      <td><span className="badge">{ACTION_LABEL[r.action] ?? r.action}</span></td>
                      <td>{r.actor_username ?? r.actor_user_id ?? "system"}</td>
                      <td>{r.target_name ?? r.target_id ?? "—"}</td>
                      <td className="muted">{r.detail ?? ""}</td>
                    </tr>
                  ))}
                  {logs.length === 0 && !logErr && (
                    <tr><td colSpan={5} className="muted" style={{ textAlign: "center", padding: 16 }}>No activity yet.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {confirmDelete && (
        <div className="modal-overlay" onClick={() => setConfirmDelete(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 420 }}>
            <h2 style={{ marginTop: 0 }}>Delete workspace &quot;{ws.name}&quot;?</h2>
            <p className="muted">
              Every file inside it moves to your Trash (kept for 30 days, restorable). Folders and
              member records are removed. This cannot be undone.
            </p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
              <button className="ghost" onClick={() => setConfirmDelete(false)}>Cancel</button>
              <button className="primary" onClick={doDelete}>Delete workspace</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Cloud drive page ───────────────────────────────────────────────────────────
export default function CloudDrive() {
  const [files, setFiles] = useState<DriveFile[]>([]);
  const [folders, setFolders] = useState<DriveFolder[]>([]);
  const [trash, setTrash] = useState<DriveFile[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [me, setMe] = useState<Me | null>(null);
  const [manageWs, setManageWs] = useState<Workspace | null>(null);
  const [loc, setLoc] = useState<Loc>({ kind: "root" });
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [viewMode, setViewMode] = useState<"list" | "grid">("list");
  const [editMode, setEditMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState("");
  const [shareTarget, setShareTarget] = useState<DriveFile | null>(null);
  const [renameTarget, setRenameTarget] = useState<DriveFile | null>(null);
  const [moveTargets, setMoveTargets] = useState<DriveFile[]>([]);
  const [newFolderLoc, setNewFolderLoc] = useState<Loc | null>(null);
  const [newTextLoc, setNewTextLoc] = useState<Loc | null>(null);
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null);
  // Note editor: text files (.md/.txt/…) open in an in-page Markdown editor instead of a tab.
  const [editing, setEditing] = useState<DriveFile | null>(null);
  // In-window Office preview (docx/xlsx/csv/tsv/pptx/…). Mutually exclusive with the editor.
  const [previewFile, setPreviewFile] = useState<DriveFile | null>(null);
  const [draft, setDraft] = useState("");
  const [dirty, setDirty] = useState(false);
  const [preview, setPreview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmPurge, setConfirmPurge] = useState(false);
  const [confirmEmptyTrash, setConfirmEmptyTrash] = useState(false);
  const [confirmFolderDelete, setConfirmFolderDelete] = useState<DriveFolder | null>(null);
  const [query, setQuery] = useState("");
  const [searchScope, setSearchScope] = useState("all"); // "all" = every file, else a workspace id
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const [f, w, d, t] = await Promise.all([
        api.listFiles(),
        api.listWorkspaces(),
        api.listFolders(),
        api.listTrash(),
      ]);
      setFiles(f.files);
      setWorkspaces(w.workspaces);
      setFolders(d.folders);
      setTrash(t.files);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  // "Import to Knowledge": (re)push a READY file into the searchable corpus. Uploads already
  // auto-enqueue; this is the explicit retry / re-import (e.g. after a config change).
  // Per-file feedback: "importing" while the job is enqueued, then "ok" / "err" for the outcome.
  const [importState, setImportState] = useState<
    Record<string, "importing" | "ok" | "err">
  >({});
  const [importError, setImportError] = useState<Record<string, string>>({});
  // When a file is first seen mid-ingest, remember the wall-clock time so the button can
  // show an approximate ~Ns countdown (see ingestEtaRemaining).
  const [ingestStart, setIngestStart] = useState<Record<string, number>>({});
  const importToRepo = useCallback(
    async (f: DriveFile) => {
      setImportState((s) => ({ ...s, [f.id]: "importing" }));
      try {
        await api.importRagFile(f.id);
        setImportState((s) => ({ ...s, [f.id]: "ok" }));
        await load();
      } catch (e) {
        setImportState((s) => ({ ...s, [f.id]: "err" }));
        setImportError((m) => ({ ...m, [f.id]: String(e) }));
      }
    },
    [load]
  );

  // Current user id, for gating workspace-owner controls in the Manage modal.
  useEffect(() => {
    api.me().then(setMe).catch(() => {});
  }, []);

  // Auto-refresh while any file is mid-ingest, so the RAG badge settles.
  useEffect(() => {
    if (!files.some((f) => RAG_WORKING.has(f.rag_status))) return;
    const t = window.setInterval(load, REFRESH_MS);
    return () => window.clearInterval(t);
  }, [files, load]);

  // Record the start time (once) when a file enters a WORKING phase, for the countdown.
  useEffect(() => {
    let changed = false;
    const next: Record<string, number> = { ...ingestStart };
    for (const f of files) {
      if (RAG_WORKING.has(f.rag_status) && !(f.id in next)) {
        next[f.id] = Date.now();
        changed = true;
      }
    }
    if (changed) setIngestStart(next);
  }, [files, ingestStart]);

  const curKey = locKey(loc);

  // Moving to another folder clears the row selection.
  useEffect(() => {
    setSelected(new Set());
  }, [curKey]);

  // Close the right-click menu on outside clicks, Escape, scroll, or window blur.
  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", onKey);
    window.addEventListener("blur", close);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", close);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [ctxMenu]);

  // Drop ids that disappeared (deleted elsewhere / refresh races).
  useEffect(() => {
    setSelected((prev) => {
      const ids = new Set(files.map((f) => f.id));
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (ids.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [files]);

  // Union of every folder path in a scope: explicit folder rows + paths derived from
  // file folder_path prefixes, so the tree shows intermediate nodes even if a folder
  // row for them does not exist yet.
  const scopePaths = useCallback(
    (ws: string | null): Set<string> => {
      const set = new Set<string>();
      for (const d of folders) {
        if ((ws == null ? d.workspace_id == null : d.workspace_id === ws) && d.path) {
          set.add(d.path);
        }
      }
      for (const f of files) {
        if (!f.folder_path) continue;
        if (ws == null ? f.workspace_id != null : f.workspace_id !== ws) continue;
        const segs = f.folder_path.split("/").filter(Boolean);
        let acc = "";
        for (const s of segs) {
          acc = acc ? `${acc}/${s}` : s;
          set.add(acc);
        }
      }
      return set;
    },
    [folders, files]
  );

  // Tree: My Drive root + one folder per workspace, each with its own subfolders,
  // plus a Trash node at the bottom.
  const treeItems = useMemo<TreeItem[]>(() => {
    return [
      {
        key: "root",
        name: "My Drive",
        icon: "☁️",
        loc: { kind: "root" } as Loc,
        children: buildTree([...scopePaths(null)], null),
      },
      ...workspaces.map((w) => ({
        key: `ws:${w.id}`,
        name: w.name,
        icon: "📁",
        loc: { kind: "workspace", id: w.id } as Loc,
        children: buildTree([...scopePaths(w.id)], w.id),
      })),
      {
        key: "trash",
        name: "Trash",
        icon: "🗑",
        loc: { kind: "trash" } as Loc,
        children: [],
      },
    ];
  }, [workspaces, scopePaths]);

  // Files shown in the right pane, matching the current location (trash view shows
  // the trash list instead of active files).
  const visibleFiles = useMemo(() => {
    if (loc.kind === "trash") return [];
    return files
      .filter((f) => {
        const fp = f.folder_path ?? "";
        if (loc.kind === "root") return f.workspace_id == null && fp === "";
        if (loc.kind === "workspace") return f.workspace_id === loc.id && fp === "";
        return (
          (loc.ws == null ? f.workspace_id == null : f.workspace_id === loc.ws) && fp === loc.path
        );
      })
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [files, loc]);

  // Direct subfolders of the current location, rendered above the file list.
  const visibleFolders = useMemo(() => {
    if (loc.kind === "trash") return [];
    const base = loc.kind === "folder" ? loc.path : "";
    const ws = loc.kind === "folder" ? loc.ws : loc.kind === "workspace" ? loc.id : null;
    return folders
      .filter((d) => (ws == null ? d.workspace_id == null : d.workspace_id === ws))
      .filter((d) => {
        const parent = d.path.includes("/") ? d.path.slice(0, d.path.lastIndexOf("/")) : "";
        return parent === base;
      })
      .sort((a, b) => a.path.localeCompare(b.path));
  }, [folders, loc]);

  const trashFiles = useMemo(
    () => (loc.kind === "trash" ? trash : []).sort((a, b) => (a.deleted_at ?? "").localeCompare(b.deleted_at ?? "")).reverse(),
    [loc, trash]
  );

  const selectedFiles = useMemo(
    () => visibleFiles.filter((f) => selected.has(f.id)),
    [visibleFiles, selected]
  );
  const selectedTrash = useMemo(
    () => trashFiles.filter((f) => selected.has(f.id)),
    [trashFiles, selected]
  );

  const allChecked =
    (loc.kind === "trash" ? trashFiles : visibleFiles).length > 0 &&
    (loc.kind === "trash" ? trashFiles : visibleFiles).every((f) => selected.has(f.id));

  const toggleAll = () => {
    const list = loc.kind === "trash" ? trashFiles : visibleFiles;
    setSelected((prev) => {
      const next = new Set(prev);
      if (allChecked) {
        for (const f of list) next.delete(f.id);
      } else {
        for (const f of list) next.add(f.id);
      }
      return next;
    });
  };

  const toggleOne = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggle = (key: string) =>
    setCollapsed((c) => {
      const n = new Set(c);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });

  const selectLoc = (l: Loc) => setLoc(l);

  // Open the Manage modal for the workspace the current location is inside of.
  const openManage = () => {
    const wsId = loc.kind === "workspace" ? loc.id : loc.kind === "folder" ? loc.ws : null;
    const w = wsId ? workspaces.find((x) => x.id === wsId) : null;
    if (w) setManageWs(w);
  };

  // Reload after member/settings changes; if the viewed workspace was deleted, drop to My Drive.
  const onManageChanged = async () => {
    await load();
    const wsId = loc.kind === "workspace" ? loc.id : loc.kind === "folder" ? loc.ws : null;
    if (wsId && !workspaces.some((x) => x.id === wsId)) setLoc({ kind: "root" });
  };

  const toggleEditMode = () => {
    if (editMode) setSelected(new Set());
    setEditMode(!editMode);
  };

  const enterFolder = (d: DriveFolder) =>
    selectLoc({ kind: "folder", ws: d.workspace_id, path: d.path });

  const upload = async (file: File) => {
    setError("");
    setMsg("");
    if (file.size > MAX_BROWSER_UPLOAD) {
      setError("Files larger than 256MB are handled in the desktop client.");
      return;
    }
    const workspace_id = loc.kind === "folder" ? loc.ws : loc.kind === "workspace" ? loc.id : null;
    const folder_path = loc.kind === "folder" ? loc.path : null;
    setUploading(true);
    try {
      setUploadProgress("Hashing…");
      const buf = await file.arrayBuffer();
      const hex = toHex(await crypto.subtle.digest("SHA-256", buf));
      const init = await api.initUpload({
        sha256: hex,
        size: file.size,
        name: file.name,
        folder_path,
        mime_type: file.type || null,
        workspace_id,
      });
      if (init.status === "instant") {
        setMsg("Uploaded instantly (deduplicated).");
        await load();
        return;
      }
      const assetId = init.asset_id!;
      const chunkSize = init.chunk_size!;
      const num = init.num_chunks!;
      for (let i = 0; i < num; i++) {
        const start = i * chunkSize;
        const slice = buf.slice(start, Math.min(file.size, start + chunkSize));
        await api.uploadChunk(assetId, i, new Blob([slice]));
        setUploadProgress(`Uploading ${i + 1}/${num}…`);
      }
      const done = await api.completeUpload(assetId);
      setMsg(done.job_id ? "Uploaded. Indexing in background…" : "Upload complete.");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setUploading(false);
      setUploadProgress("");
    }
  };

  const download = async (f: DriveFile) => {
    setError("");
    try {
      const blob = await api.downloadFile(f.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = f.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e));
    }
  };

  const downloadSelected = async () => {
    setError("");
    try {
      for (const f of selectedFiles) await download(f);
    } catch (e) {
      setError(String(e));
    }
  };

  // Open a file in a new tab (browser download + display for viewable types).
  const openFile = async (f: DriveFile) => {
    setError("");
    try {
      const blob = await api.downloadFile(f.id);
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank");
    } catch (e) {
      setError(String(e));
    }
  };

  // ── Note editor (Markdown text files) ──────────────────────────────────────
  const isTextFile = (f: DriveFile) => {
    const name = f.name || "";
    // .csv/.tsv are delimited tables — they open in the in-window table preview, not
    // the note editor, even if the server stored them with a text/* mime type.
    if (/\.(csv|tsv)$/i.test(name)) return false;
    const mime = (f.mime_type || "").toLowerCase();
    if (mime.startsWith("text/")) return true;
    return /\.(txt|md|markdown|text|log|json|yaml|yml|toml|ini|xml|html|py|js|ts|jsx|tsx|c|h|cpp|hpp|java|go|rs|sh|bat|sql)$/i.test(
      name
    );
  };

  const openNote = async (f: DriveFile) => {
    setError("");
    try {
      const { content } = await api.getFileContent(f.id);
      setEditing(f);
      setDraft(content);
      setDirty(false);
      setPreview(false);
    } catch (e) {
      setError(String(e));
    }
  };

  const openEntry = (f: DriveFile) => {
    if (editMode) return toggleOne(f.id);
    if (isTrash) return;
    if (officeKindOf(f.name)) return setPreviewFile(f);
    if (isTextFile(f)) return openNote(f);
    return openFile(f);
  };

  const saveNote = async () => {
    if (!editing) return;
    setSaving(true);
    try {
      const { asset } = await api.updateFileContent(editing.id, draft);
      setFiles((prev) => prev.map((x) => (x.id === asset.id ? asset : x)));
      setEditing((prev) => (prev && prev.id === asset.id ? asset : prev));
      setDirty(false);
      setMsg("Note saved. Re-indexing in background…");
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const closeNote = () => {
    if (dirty && !window.confirm("Discard unsaved changes?")) return;
    setEditing(null);
    setDraft("");
    setDirty(false);
    setPreview(false);
  };

  const openSelected = async () => {
    const f = selectedFiles[0];
    if (!f) return;
    if (officeKindOf(f.name)) return setPreviewFile(f);
    if (isTextFile(f)) return openNote(f);
    await openFile(f);
  };

  // Delete = move to the trash (bytes + sharing kept so it can be restored).
  const removeSelected = async () => {
    setError("");
    try {
      for (const f of selectedFiles) await api.deleteFile(f.id);
      setConfirmDelete(false);
      setSelected(new Set());
      setMsg(`Moved ${selectedFiles.length} file${selectedFiles.length > 1 ? "s" : ""} to Trash.`);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const deleteFolder = async (d: DriveFolder) => {
    setError("");
    try {
      await api.deleteFolder(d.id);
      setConfirmFolderDelete(null);
      setMsg(`Folder "${d.name}" deleted; its files moved to Trash.`);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const restoreSelected = async () => {
    setError("");
    try {
      for (const f of selectedTrash) await api.restoreTrash(f.id);
      setSelected(new Set());
      setMsg(`Restored ${selectedTrash.length} file${selectedTrash.length > 1 ? "s" : ""}.`);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const purgeSelected = async () => {
    setError("");
    try {
      for (const f of selectedTrash) await api.purgeTrash(f.id);
      setConfirmPurge(false);
      setSelected(new Set());
      setMsg(`Permanently deleted ${selectedTrash.length} file${selectedTrash.length > 1 ? "s" : ""}.`);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const emptyTrashAll = async () => {
    setError("");
    try {
      const r = await api.emptyTrash();
      setConfirmEmptyTrash(false);
      setSelected(new Set());
      setMsg(`Trash emptied (${r.purged} files permanently deleted).`);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const newWorkspace = async () => {
    const name = window.prompt("New workspace name");
    if (!name?.trim()) return;
    setError("");
    try {
      await api.createWorkspace(name.trim());
      await load();
      setMsg("Workspace created.");
    } catch (e) {
      setError(String(e));
    }
  };

  // ── Search (client-side: all files are already loaded) ───────────────────────
  const suggestions = useMemo(() => {
    if (!query.trim()) return [];
    const q = query.trim();
    const candidates =
      searchScope === "all" ? files : files.filter((f) => f.workspace_id === searchScope);
    const scored: { f: DriveFile; score: number }[] = [];
    for (const f of candidates) {
      const n = fuzzyScore(f.name, q);
      const p = f.folder_path ? fuzzyScore(f.folder_path, q) : null;
      const score = n != null ? n : p != null ? 1000 + p : null;
      if (score != null) scored.push({ f, score });
    }
    scored.sort((a, b) => a.score - b.score);
    return scored.slice(0, 10);
  }, [files, query, searchScope]);

  const wsName = (id: string | null) =>
    id ? workspaces.find((w) => w.id === id)?.name ?? "Workspace" : "My Drive";

  const jumpTo = (f: DriveFile) => {
    if (f.workspace_id) {
      setLoc(
        f.folder_path
          ? { kind: "folder", ws: f.workspace_id, path: f.folder_path }
          : { kind: "workspace", id: f.workspace_id }
      );
    } else {
      setLoc(f.folder_path ? { kind: "folder", ws: null, path: f.folder_path } : { kind: "root" });
    }
    setQuery("");
  };

  const ragClass = (s: string) =>
    s === "INDEXED" ? "rag-indexed" : s === "FAILED" ? "rag-failed" : RAG_WORKING.has(s) ? "rag-working" : "rag-pending";

  const isTrash = loc.kind === "trash";
  const list = isTrash ? trashFiles : visibleFiles;

  // Permission gates for the current view. My Drive and Trash are personal, so the
  // user always has full access there; inside a workspace the role decides:
  //   - canWrite  : owner / editor → Upload, New folder, Move, Share, Rename, Delete
  //   - canManage : owner only     → ⚙ Manage (members + settings)
  const curWsId = loc.kind === "workspace" ? loc.id : loc.kind === "folder" ? loc.ws : null;
  const curWs = curWsId ? workspaces.find((w) => w.id === curWsId) ?? null : null;
  // Roles per workspace: owner > admin > editor > viewer.
  //  - canWrite  : owner / admin / editor → Upload, New folder, Move, Share, Rename, Delete
  //  - canManage : owner / admin → ⚙ Manage (members + logs)
  //  - workspace rename/delete stays owner-only (see Manage modal).
  const canWrite = !curWs || curWs.role === "owner" || curWs.role === "admin" || curWs.role === "editor";
  const canManage = !curWs || curWs.role === "owner" || curWs.role === "admin";

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>☁️ Cloud Drive</h3>

      <div className="drive-layout">
        <aside className="drive-tree panel">
          <div className="drive-tree-header">
            <span className="muted" style={{ fontSize: 12, flex: 1 }}>Folders</span>
            <button className="ghost" onClick={newWorkspace} title="New workspace">＋ New workspace</button>
          </div>
          <DriveTree
            items={treeItems}
            collapsed={collapsed}
            selectedKey={curKey}
            onSelect={selectLoc}
            onToggle={toggle}
          />
        </aside>

        <section
          className="drive-main panel"
          onContextMenu={(e) => {
            if (isTrash) return;
            e.preventDefault();
            setCtxMenu({ x: e.clientX, y: e.clientY });
          }}
        >
          <div className="drive-toolbar">
            <span className="drive-path muted" title={locLabel(loc, workspaces)}>
              {locLabel(loc, workspaces)}
            </span>
            <span className="drive-view-toggle">
              <button
                className={viewMode === "list" ? "active" : ""}
                onClick={() => setViewMode("list")}
                title="List view"
              >☰</button>
              <button
                className={viewMode === "grid" ? "active" : ""}
                onClick={() => setViewMode("grid")}
                title="Grid view"
              >▦</button>
            </span>

            <div className="drive-search-wrap">
              <select
                className="drive-search-scope"
                value={searchScope}
                onChange={(e) => setSearchScope(e.target.value)}
                title="Search scope"
              >
                <option value="all">All files (My Drive)</option>
                {workspaces.map((w) => (
                  <option key={w.id} value={w.id}>{w.name}</option>
                ))}
              </select>
              <input
                className="drive-search"
                placeholder="Search files…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              {suggestions.length > 0 && (
                <div className="drive-suggest">
                  {suggestions.map(({ f }) => (
                    <div
                      className="drive-suggest-item"
                      key={f.id}
                      onMouseDown={(e) => {
                        e.preventDefault();
                        jumpTo(f);
                      }}
                    >
                      <span className="drive-file-icon">📄</span>
                      <span className="drive-suggest-name">{f.name}</span>
                      <span className="muted drive-suggest-meta">
                        {wsName(f.workspace_id)}
                        {f.folder_path ? ` / ${f.folder_path}` : ""}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <span style={{ flex: 1 }} />
            {(loc.kind === "workspace" || (loc.kind === "folder" && loc.ws != null)) && (
              <button
                className="ghost"
                onClick={openManage}
                disabled={!canManage}
                title={canManage ? "Manage workspace members and settings" : "Only the workspace owner can manage members"}
              >⚙ Manage</button>
            )}
            <button
              className="ghost"
              onClick={toggleEditMode}
              title={editMode ? "Done selecting" : "Show selection checkboxes"}
            >
              {editMode ? "✓ Done" : "✏ Edit"}
            </button>
            {isTrash ? (
              <button className="ghost danger" onClick={() => setConfirmEmptyTrash(true)}>
                Empty Trash
              </button>
            ) : (
              <>
                <button
                  className="ghost"
                  onClick={() => setNewFolderLoc(loc)}
                  disabled={!canWrite}
                  title={canWrite ? "Create a new folder here" : "You don't have write access here"}
                >＋ New folder</button>
                <button
                  className="primary"
                  onClick={() => fileRef.current?.click()}
                  disabled={uploading || !canWrite}
                  title={canWrite ? "Upload a file here" : "You don't have write access here"}
                >
                  {uploading ? (uploadProgress || "Uploading…") : "⬆ Upload"}
                </button>
              </>
            )}
            <input
              ref={fileRef}
              type="file"
              style={{ display: "none" }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) upload(f);
                e.target.value = "";
              }}
            />
          </div>

          {msg && <p className="success" style={{ fontSize: 13 }}>{msg}</p>}
          {error && <p className="error" style={{ fontSize: 13 }}>{error}</p>}

          {editMode && (
            <div className="drive-batchbar">
              <span className="muted" style={{ marginRight: 8 }}>
                {(isTrash ? selectedTrash : selectedFiles).length} selected
              </span>
              {isTrash ? (
                <>
                  <button
                    className="ghost"
                    onClick={restoreSelected}
                    disabled={selectedTrash.length === 0}
                  >↩ Restore</button>
                  <button
                    className="ghost danger"
                    onClick={() => setConfirmPurge(true)}
                    disabled={selectedTrash.length === 0}
                  >✖ Delete permanently</button>
                </>
              ) : (
                <>
                  <button
                    className="ghost"
                    onClick={downloadSelected}
                    disabled={selectedFiles.length === 0}
                  >⬇ Download</button>
                  <button
                    className="ghost"
                    onClick={openSelected}
                    disabled={selectedFiles.length !== 1}
                    title="Open in a new tab"
                  >↗ Open</button>
                  <button
                    className="ghost"
                    onClick={() => setShareTarget(selectedFiles[0])}
                    disabled={selectedFiles.length !== 1 || !canWrite}
                    title={canWrite ? "Share this file" : "You don't have write access here"}
                  >🔗 Share</button>
                  <button
                    className="ghost"
                    onClick={() => setRenameTarget(selectedFiles[0])}
                    disabled={selectedFiles.length !== 1 || !canWrite}
                    title={canWrite ? "Rename this file" : "You don't have write access here"}
                  >✏ Rename</button>
                  <button
                    className="ghost"
                    onClick={() => setMoveTargets(selectedFiles)}
                    disabled={selectedFiles.length === 0 || !canWrite}
                    title={canWrite ? "Move to another folder or workspace" : "You don't have write access here"}
                  >⇄ Move</button>
                  <button
                    className="ghost danger"
                    onClick={() => setConfirmDelete(true)}
                    disabled={selectedFiles.length === 0 || !canWrite}
                    title={canWrite ? "Move selected files to Trash" : "You don't have write access here"}
                  >🗑 Delete</button>
                </>
              )}
            </div>
          )}

          {previewFile ? (
            <FilePreview
              file={previewFile}
              onClose={() => setPreviewFile(null)}
              onDownload={() => download(previewFile)}
              onOpen={() => openFile(previewFile)}
            />
          ) : editing ? (
            <div className="note-editor">
              <div className="note-editor-toolbar">
                <span className="note-editor-title" title={editing.folder_path ?? ""}>
                  📄 {editing.name}
                  {dirty && <span className="muted"> • unsaved</span>}
                </span>
                <span style={{ flex: 1 }} />
                <button
                  className={preview ? "" : "active"}
                  onClick={() => setPreview(false)}
                  title="Edit Markdown source"
                >✏ Edit</button>
                <button
                  className={preview ? "active" : ""}
                  onClick={() => setPreview(true)}
                  title="Preview rendered Markdown"
                >👁 Preview</button>
                <button
                  className="primary"
                  onClick={saveNote}
                  disabled={!dirty || saving}
                  title={dirty ? "Save changes" : "No unsaved changes"}
                >{saving ? "Saving…" : "💾 Save"}</button>
                <button className="ghost" onClick={closeNote} title="Close note">✖</button>
              </div>
              {preview ? (
                <div className="md-preview" dangerouslySetInnerHTML={{ __html: renderMarkdown(draft) }} />
              ) : (
                <textarea
                  className="note-editor-textarea"
                  value={draft}
                  onChange={(e) => {
                    setDraft(e.target.value);
                    setDirty(true);
                  }}
                  onKeyDown={(e) => {
                    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
                      e.preventDefault();
                      if (dirty && !saving) saveNote();
                    }
                  }}
                  spellCheck={false}
                  placeholder="Type Markdown here…"
                />
              )}
            </div>
          ) : list.length === 0 && visibleFolders.length === 0 ? (
            <p className="muted" style={{ padding: "16px 4px" }}>
              {isTrash
                ? "Trash is empty."
                : files.length === 0
                  ? "No files yet. Upload your first file."
                  : "This folder is empty."}
            </p>
          ) : viewMode === "grid" ? (
            <div className="drive-grid">
              {visibleFolders.map((d) => (
                <div
                  key={d.id}
                  className="drive-tile drive-dir-tile"
                  onDoubleClick={() => enterFolder(d)}
                  title="Double-click to enter"
                >
                  <div className="drive-tile-icon">📁</div>
                  <div className="drive-tile-name" title={d.name}>{d.name}</div>
                  <div className="drive-tile-meta">
                    <span className="muted">Folder</span>
                  </div>
                </div>
              ))}
              {list.map((f) => (
                <div
                  key={f.id}
                  className={"drive-tile" + (selected.has(f.id) ? " selected" : "")}
                  onClick={() => openEntry(f)}
                  title={editMode ? (selected.has(f.id) ? "Click to deselect" : "Click to select") : "Click to open"}
                >
                  {editMode && (
                    <input
                      type="checkbox"
                      className="drive-tile-check"
                      checked={selected.has(f.id)}
                      onChange={() => toggleOne(f.id)}
                      onClick={(e) => e.stopPropagation()}
                    />
                  )}
                  <div className="drive-tile-icon">📄</div>
                  <div className="drive-tile-name" title={f.name}>{f.name}</div>
                  <div className="drive-tile-meta">
                    <span className="muted">{fmtSize(f.size)}</span>
                    {isTrash ? (
                      <span className="muted">{fmtDate(f.deleted_at)}</span>
                    ) : (
                      <span className={`badge rag ${ragClass(f.rag_status)}`}>
                        {RAG_LABEL[f.rag_status] ?? f.rag_status}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <>
              {visibleFolders.length > 0 && (
                <div className="drive-dir-list">
                  {visibleFolders.map((d) => (
                    <div
                      key={d.id}
                      className="drive-dir-row"
                      onDoubleClick={() => enterFolder(d)}
                      title="Double-click to enter"
                    >
                      <span className="drive-folder-icon">📁</span>
                      <span className="drive-node-name">{d.name}</span>
                      <span style={{ flex: 1 }} />
                      {editMode && (
                        <button
                          className="ghost danger drive-dir-del"
                          onClick={() => setConfirmFolderDelete(d)}
                          disabled={!canWrite}
                          title={canWrite ? "Delete folder (its files move to Trash)" : "You don't have write access here"}
                        >🗑</button>
                      )}
                    </div>
                  ))}
                </div>
              )}
              <table className="st-table">
                <thead>
                  <tr>
                    {editMode && (
                      <th style={{ width: 32 }}>
                        <input
                          type="checkbox"
                          checked={allChecked}
                          onChange={toggleAll}
                          title="Select all files in this folder"
                        />
                      </th>
                    )}
                    <th>Name</th>
                    {isTrash ? <th>Deleted</th> : <th>Size</th>}
                    {!isTrash && <th>RAG Status</th>}
                    {!isTrash && <th>Query Repo</th>}
                    <th>Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((f) => (
                    <tr
                      key={f.id}
                      className={selected.has(f.id) ? "drive-row-selected" : ""}
                      onClick={() => openEntry(f)}
                      title={editMode ? "" : isTrash ? "" : "Click to open"}
                    >
                      {editMode && (
                        <td onClick={(e) => e.stopPropagation()}>
                          <input type="checkbox" checked={selected.has(f.id)} onChange={() => toggleOne(f.id)} />
                        </td>
                      )}
                      <td>
                        <span className="drive-file-icon">📄</span> {f.name}
                      </td>
                      {isTrash ? (
                        <td className="muted">{fmtDate(f.deleted_at)}</td>
                      ) : (
                        <td className="muted">{fmtSize(f.size)}</td>
                      )}
                      {!isTrash && (
                        <td>
                          <span className={`badge rag ${ragClass(f.rag_status)}`}>
                            {RAG_LABEL[f.rag_status] ?? f.rag_status}
                          </span>
                        </td>
                      )}
                      {!isTrash && (
                        <td onClick={(e) => e.stopPropagation()}>
                          {f.rag_status === "INDEXED" ? (
                            <button className="ghost" disabled title="Already in knowledge">
                              ✓ In Knowledge
                            </button>
                          ) : importState[f.id] === "importing" ? (
                            <button
                              className="ghost"
                              disabled
                              title="Importing into the searchable corpus…"
                            >
                              Importing…
                            </button>
                          ) : RAG_WORKING.has(f.rag_status) ? (
                            <button
                              className="ghost"
                              disabled
                              title="Already queued / processing — flips to In Knowledge when done"
                            >
                              {f.rag_status === "PENDING"
                                ? "Queued…"
                                : `Processing…${ingestEtaSuffix(f.size, f.name, ingestStart[f.id])}`}
                            </button>
                          ) : RAG_IMPORTABLE_EXTS.has(fileExt(f.name)) ? (
                            <button
                              className={`ghost${importState[f.id] === "err" ? " danger" : ""}`}
                              onClick={() => importToRepo(f)}
                              title={
                                importState[f.id] === "err"
                                  ? importError[f.id] || "Import failed"
                                  : "Import this file into your searchable knowledge"
                              }
                            >
                              {importState[f.id] === "err"
                                ? "Failed — retry"
                                : "＋ Import to Knowledge"}
                            </button>
                          ) : (
                            <button className="ghost" disabled title="Format not supported">
                              Not supported
                            </button>
                          )}
                        </td>
                      )}
                      <td className="muted">{fmtDate(f.updated_at ?? f.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </section>
      </div>

      {ctxMenu && (
        <div
          className="drive-ctxmenu"
          style={{
            left: Math.min(ctxMenu.x, window.innerWidth - 200),
            top: Math.min(ctxMenu.y, window.innerHeight - 120),
          }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          <button
            disabled={!canWrite}
            onClick={() => {
              setCtxMenu(null);
              setNewTextLoc(loc);
            }}
            title={canWrite ? "Create a new text file here" : "You don't have write access here"}
          >📄 New text file</button>
          <button
            disabled={!canWrite}
            onClick={() => {
              setCtxMenu(null);
              setNewFolderLoc(loc);
            }}
            title={canWrite ? "Create a new folder here" : "You don't have write access here"}
          >📁 New folder</button>
        </div>
      )}

      {shareTarget && <ShareModal file={shareTarget} onClose={() => setShareTarget(null)} />}

      {renameTarget && (
        <RenameModal
          file={renameTarget}
          onClose={() => setRenameTarget(null)}
          onSaved={async () => {
            setRenameTarget(null);
            setMsg("File renamed.");
            await load();
          }}
        />
      )}

      {moveTargets.length > 0 && (
        <MoveModal
          files={moveTargets}
          workspaces={workspaces}
          defaultWs={loc.kind === "folder" ? loc.ws : loc.kind === "workspace" ? loc.id : null}
          defaultPath={loc.kind === "folder" ? loc.path : ""}
          onClose={() => setMoveTargets([])}
          onMoved={async () => {
            setMoveTargets([]);
            setSelected(new Set());
            setMsg(`Moved ${moveTargets.length} file${moveTargets.length > 1 ? "s" : ""}.`);
            await load();
          }}
        />
      )}

      {newFolderLoc && (
        <NewFolderModal
          loc={newFolderLoc}
          onClose={() => setNewFolderLoc(null)}
          onCreated={async (requested, finalName) => {
            setNewFolderLoc(null);
            setMsg(
              requested !== finalName
                ? `"${requested}" already exists — created as "${finalName}".`
                : "Folder created."
            );
            await load();
          }}
        />
      )}

      {newTextLoc && (
        <NewTextModal
          loc={newTextLoc}
          onClose={() => setNewTextLoc(null)}
          onCreated={async (requested, finalName) => {
            setNewTextLoc(null);
            setMsg(
              requested !== finalName
                ? `"${requested}" already exists — created as "${finalName}".`
                : "Text file created."
            );
            await load();
          }}
        />
      )}

      {confirmDelete && (
        <div className="modal-overlay" onClick={() => setConfirmDelete(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 420 }}>
            <h2 style={{ marginTop: 0 }}>
              Move {selectedFiles.length > 1 ? `${selectedFiles.length} files` : "file"} to Trash?
            </h2>
            <p className="muted">
              {selectedFiles.length > 1
                ? "The selected files move to the Trash. They are kept for 30 days (sharing and content preserved), then deleted permanently — or you can restore them any time."
                : `"${selectedFiles[0]?.name}" moves to the Trash. It is kept for 30 days (sharing and content preserved), then deleted permanently — or you can restore it any time.`}
            </p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
              <button className="ghost" onClick={() => setConfirmDelete(false)}>Cancel</button>
              <button className="primary" onClick={removeSelected}>Move to Trash</button>
            </div>
          </div>
        </div>
      )}

      {confirmPurge && (
        <div className="modal-overlay" onClick={() => setConfirmPurge(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 420 }}>
            <h2 style={{ marginTop: 0 }}>Permanently delete?</h2>
            <p className="muted">
              {selectedTrash.length > 1
                ? `${selectedTrash.length} files will be permanently deleted. This cannot be undone.`
                : `"${selectedTrash[0]?.name}" will be permanently deleted. This cannot be undone.`}
            </p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
              <button className="ghost" onClick={() => setConfirmPurge(false)}>Cancel</button>
              <button className="primary" onClick={purgeSelected}>Delete permanently</button>
            </div>
          </div>
        </div>
      )}

      {confirmEmptyTrash && (
        <div className="modal-overlay" onClick={() => setConfirmEmptyTrash(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 420 }}>
            <h2 style={{ marginTop: 0 }}>Empty Trash?</h2>
            <p className="muted">
              Everything in the Trash ({trashFiles.length} file{trashFiles.length === 1 ? "" : "s"}) will be
              permanently deleted. This cannot be undone.
            </p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
              <button className="ghost" onClick={() => setConfirmEmptyTrash(false)}>Cancel</button>
              <button className="primary" onClick={emptyTrashAll}>Empty Trash</button>
            </div>
          </div>
        </div>
      )}

      {confirmFolderDelete && (
        <div className="modal-overlay" onClick={() => setConfirmFolderDelete(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 420 }}>
            <h2 style={{ marginTop: 0 }}>Delete folder &quot;{confirmFolderDelete.name}&quot;?</h2>
            <p className="muted">
              Every file inside it moves to the Trash (kept for 30 days, restorable). The folder is removed.
            </p>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
              <button className="ghost" onClick={() => setConfirmFolderDelete(null)}>Cancel</button>
              <button className="primary" onClick={() => deleteFolder(confirmFolderDelete)}>Delete folder</button>
            </div>
          </div>
        </div>
      )}

      {manageWs && (
        <WorkspaceManageModal
          ws={manageWs}
          me={me}
          onClose={() => setManageWs(null)}
          onChanged={onManageChanged}
        />
      )}
    </div>
  );
}
