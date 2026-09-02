import { useEffect, useMemo, useRef, useState } from "react";
import * as XLSX from "xlsx";
import { api, clearToken, getToken, setToken, type AuthActionResponse } from "./api";
import CloudDrive from "./CloudDrive";
import MicRecorder from "./MicRecorder";
import Research from "./Research";
import { useJob } from "./useJob";
import type { Article, Domain, Me, Model, Sentence, Term, UsageReport } from "./types";

type Page = "home" | "import" | "study" | "manage";
type Tab = "learn" | "me" | "drive" | "research";

type AuthState =
  | { status: "loading" }
  | { status: "anon" }
  | { status: "authed"; user: Me };

const PAGE_SIZE = 10;

// ── Appearance prefs (theme / font size), persisted per-browser ──
const THEME_KEY = "deepdive_web_theme";
const FONT_KEY = "deepdive_web_fontsize";
const THEME_LABELS: Record<string, string> = {
  system: "Follow System",
  dark: "Dark",
  light: "Light",
};
const FONT_SIZES = [12, 13, 14, 15, 16, 17, 18];

function readPref(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function effectiveTheme(theme: string): string {
  return theme === "system"
    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : theme;
}
function applyThemePref(theme: string) {
  document.documentElement.dataset.theme = effectiveTheme(theme);
}
function applyFontSizePref(px: number) {
  document.documentElement.style.setProperty("zoom", String(px / 14));
}
// Apply before first paint so a dark/font choice does not flash.
(function initAppearance() {
  try {
    applyThemePref(readPref(THEME_KEY, "system"));
    applyFontSizePref(parseInt(readPref(FONT_KEY, "14"), 10) || 14);
  } catch { /* ignore */ }
})();

export default function App() {
  const [auth, setAuth] = useState<AuthState>({ status: "loading" });
  const [page, setPage] = useState<Page>("home");
  const [profileOpen, setProfileOpen] = useState(false);
  // Default to the Cloud Drive tab. The desktop client deep-links via ?sso=<token>#drive;
  // the hash is preserved through the SSO handoff below and stays on the drive tab.
  const [tab, setTab] = useState<Tab>("drive");

  useEffect(() => {
    // SSO handoff from the desktop client: ?sso=<token>. Store it, then drop the
    // token from the address bar so it does not linger in history/logs.
    const sso = new URLSearchParams(location.search).get("sso");
    if (sso) {
      setToken(sso);
      history.replaceState({}, "", location.pathname + location.hash);
    }
    (async () => {
      // The desktop hands over its API token via ?sso=; exchange it for a stateless
      // console session (cc_) so a later desktop re-login — which rotates the API
      // token — does not invalidate this tab. Already-console tokens pass through.
      if (getToken() && !getToken()!.startsWith("cc_")) {
        try {
          const session = await api.exchangeSession();
          setToken(session.access_token);
        } catch {
          // Exchange may 401 (helper clears the token) or fail transiently; either way
          // the /auth/me call below validates whatever remains.
        }
      }
      if (!getToken()) {
        setAuth({ status: "anon" });
        return;
      }
      try {
        const me = await api.me();
        setAuth({ status: "authed", user: me });
      } catch {
        clearToken();
        setAuth({ status: "anon" });
      }
    })();
  }, []);

  const logout = () => {
    clearToken();
    setPage("home");
    setAuth({ status: "anon" });
  };

  if (auth.status === "loading") {
    return <div className="login-wrap"><p className="muted">Loading…</p></div>;
  }
  if (auth.status === "anon") {
    return <LoginPage onLogin={(me) => setAuth({ status: "authed", user: me })} />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
      <div className="tabs" style={{ padding: "10px 16px 0", marginBottom: 0, flexShrink: 0 }}>
        <button className={tab === "drive" ? "tab active" : "tab"} onClick={() => setTab("drive")}>
          ☁️ Cloud Drive
        </button>
        <button className={tab === "learn" ? "tab active" : "tab"} onClick={() => setTab("learn")}>
          Learning Platform
        </button>
        <button className={tab === "me" ? "tab active" : "tab"} onClick={() => setTab("me")}>
          My Account
        </button>
        <button className={tab === "research" ? "tab active" : "tab"} onClick={() => setTab("research")}>
          🔬 Research
        </button>
      </div>
      <div className="topbar">
        <SettingsMenu />
        <AccountChip user={auth.user} onLogout={logout} onProfile={() => setProfileOpen(true)} />
      </div>
      {tab === "learn" ? (
        <div className="layout" style={{ flex: 1, minHeight: 0 }}>
          <Sidebar page={page} onNavigate={setPage} />
          <main className="content">
            {page === "home" && <Home />}
            {page === "import" && <ImportData />}
            {page === "study" && <StudyMode />}
            {page === "manage" && <ManageVocabulary />}
          </main>
        </div>
      ) : tab === "me" ? (
        <div className="layout" style={{ flex: 1, minHeight: 0 }}>
          <main className="content" style={{ paddingTop: 32 }}>
            <MyAccount user={auth.user} />
          </main>
        </div>
      ) : tab === "research" ? (
        <div className="layout" style={{ flex: 1, minHeight: 0 }}>
          <main className="content" style={{ paddingTop: 32 }}>
            <Research />
          </main>
        </div>
      ) : (
        <div className="layout" style={{ flex: 1, minHeight: 0 }}>
          <main className="content" style={{ paddingTop: 32 }}>
            <CloudDrive />
          </main>
        </div>
      )}
      {profileOpen && (
        <ProfileModal
          user={auth.user}
          onClose={() => setProfileOpen(false)}
          onUpdated={(me) => setAuth({ status: "authed", user: me })}
        />
      )}
    </div>
  );
}

// ── Top-right settings: theme + font size ──
function SettingsMenu() {
  const [open, setOpen] = useState(false);
  const [theme, setTheme] = useState(() => readPref(THEME_KEY, "system"));
  const [fontSize, setFontSize] = useState(() => parseInt(readPref(FONT_KEY, "14"), 10) || 14);

  // Re-resolve "system" when the OS preference changes.
  useEffect(() => {
    if (theme !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyThemePref("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  const changeTheme = (t: string) => {
    setTheme(t);
    try { localStorage.setItem(THEME_KEY, t); } catch { /* ignore */ }
    applyThemePref(t);
  };
  const changeFontSize = (px: number) => {
    setFontSize(px);
    try { localStorage.setItem(FONT_KEY, String(px)); } catch { /* ignore */ }
    applyFontSizePref(px);
  };

  return (
    <div className="settings-wrap">
      {open && <div className="settings-backdrop" onClick={() => setOpen(false)} />}
      <button className="header-btn header-menu" title="Settings" onClick={() => setOpen((o) => !o)}>
        ⚙ Settings
      </button>
      {open && (
        <div className="settings-pop">
          <div className="settings-title">Theme</div>
          {(["system", "dark", "light"] as const).map((t) => (
            <button
              key={t}
              className={"settings-row" + (theme === t ? " on" : "")}
              onClick={() => changeTheme(t)}
            >
              <span>{THEME_LABELS[t]}</span>
              <span className="check">✓</span>
            </button>
          ))}
          <div className="settings-title">Font size</div>
          <select
            className="settings-select"
            value={fontSize}
            onChange={(e) => changeFontSize(parseInt(e.target.value, 10) || 14)}
          >
            {FONT_SIZES.map((px) => (
              <option key={px} value={px}>
                {px}px
              </option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}

// ── Login (direct visits; the desktop client auto-signs-in via ?sso=) ──
// Toggles between sign-in / create-account / forgot-password on one card.
function LoginPage({ onLogin }: { onLogin: (me: Me) => void }) {
  const [mode, setMode] = useState<"login" | "register" | "forgot">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [regEmail, setRegEmail] = useState("");
  const [regDisplay, setRegDisplay] = useState("");
  const [forgotEmail, setForgotEmail] = useState("");
  const [regDone, setRegDone] = useState<AuthActionResponse | null>(null);
  const [forgotDone, setForgotDone] = useState<AuthActionResponse | null>(null);

  const switchMode = (m: typeof mode) => {
    setMode(m);
    setError("");
    setRegDone(null);
    setForgotDone(null);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      // Stateless console session (cc_): survives desktop re-logins, unlike the dd_
      // API token that /auth/login mints and the next login rotates away.
      const res = await api.sessionLogin(username.trim(), password);
      setToken(res.access_token);
      onLogin(await api.me());
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const submitRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    setRegDone(null);
    try {
      setRegDone(await api.register(username.trim(), regEmail.trim(), password, regDisplay.trim() || undefined));
      setPassword("");
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const submitForgot = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    setForgotDone(null);
    try {
      setForgotDone(await api.forgotPassword(forgotEmail.trim()));
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const debugLink = (url: string, label: string) => (
    <div className="debug-link">
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
      <input readOnly value={url} onFocus={(e) => e.currentTarget.select()} />
    </div>
  );

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-logo"><img src="/deepdive.png" className="brand-logo" alt="DeepDive" /> DeepDive</div>

        {mode === "login" && (
          <form onSubmit={submit}>
            <h2>Sign in to Web Console</h2>
            <label className="field-label">Username</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus />
            <label className="field-label">Password</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            {error && <p className="error">{error}</p>}
            <button type="submit" className="primary" disabled={busy || !username || !password}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
            <div className="account-links">
              <button type="button" className="linklike" onClick={() => switchMode("register")}>Create account</button>
              <button type="button" className="linklike" onClick={() => switchMode("forgot")}>Forgot password?</button>
            </div>
            <p className="muted" style={{ margin: "12px 0 0", textAlign: "center", fontSize: 12 }}>
              Tip: open Web Console from the desktop app to sign in automatically.
            </p>
          </form>
        )}

        {mode === "register" && (
          <form onSubmit={submitRegister}>
            <h2>Create account</h2>
            <p className="muted" style={{ fontSize: 12 }}>验证邮件会发送到你的邮箱,验证后才能登录。</p>
            <label className="field-label">Username</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus />
            <label className="field-label">Email</label>
            <input type="email" value={regEmail} onChange={(e) => setRegEmail(e.target.value)} autoComplete="email" />
            <label className="field-label">Display name (optional)</label>
            <input value={regDisplay} onChange={(e) => setRegDisplay(e.target.value)} autoComplete="name" />
            <label className="field-label">Password</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
            {error && <p className="error">{error}</p>}
            {regDone && (
              <div className="auth-done">
                <p className="success">{regDone.message}</p>
                {regDone.debug_verify_url && debugLink(regDone.debug_verify_url, "开发者验证链接(SMTP 未配置)")}
              </div>
            )}
            <button type="submit" className="primary" disabled={busy || !username || !regEmail || !password}>
              {busy ? "Creating…" : "Create account"}
            </button>
            <button type="button" className="linklike" onClick={() => switchMode("login")}>← Back to sign in</button>
          </form>
        )}

        {mode === "forgot" && (
          <form onSubmit={submitForgot}>
            <h2>Reset password</h2>
            <p className="muted" style={{ fontSize: 12 }}>输入注册邮箱,我们会发送一个重置链接。</p>
            <label className="field-label">Email</label>
            <input type="email" value={forgotEmail} onChange={(e) => setForgotEmail(e.target.value)} autoComplete="email" autoFocus />
            {error && <p className="error">{error}</p>}
            {forgotDone && (
              <div className="auth-done">
                <p className="success">{forgotDone.message}</p>
                {forgotDone.debug_verify_url && debugLink(forgotDone.debug_verify_url, "开发者重置链接(SMTP 未配置)")}
              </div>
            )}
            <button type="submit" className="primary" disabled={busy || !forgotEmail}>
              {busy ? "Sending…" : "Send reset link"}
            </button>
            <button type="button" className="linklike" onClick={() => switchMode("login")}>← Back to sign in</button>
          </form>
        )}
      </div>
    </div>
  );
}

// ── Top-right identity: avatar + username + role badge ──
function AccountChip({ user, onLogout, onProfile }: { user: Me; onLogout: () => void; onProfile: () => void }) {
  const name = user.display_name || user.username;
  const initial = name ? name.trim()[0]?.toUpperCase() : "?";
  return (
    <div className="account-chip">
      {user.avatar ? (
        <img className="account-avatar avatar-img" src={user.avatar} alt="" referrerPolicy="no-referrer" />
      ) : (
        <span className="account-avatar">{initial}</span>
      )}
      <span className="account-name" title={name}>{user.username}</span>
      <span className={"account-role tier" + (user.role_id === "admin" ? " vip" : "")}>
        {user.role_name}
      </span>
      <button className="header-btn" onClick={onProfile} title="Edit profile">Profile</button>
      <button className="header-btn" onClick={onLogout} title="Sign out">Sign out</button>
    </div>
  );
}

// ── Profile modal: edit display name / username / email / phone / password / avatar ──
function ProfileModal({ user, onClose, onUpdated }: { user: Me; onClose: () => void; onUpdated: (me: Me) => void }) {
  const [me, setMe] = useState<Me>(user);
  const [display, setDisplay] = useState(user.display_name || "");
  const [username, setUsername] = useState(user.username);
  const [email, setEmail] = useState(user.email || "");
  const [phone, setPhone] = useState(user.phone || "");
  const [curpass, setCurpass] = useState("");
  const [newpass, setNewpass] = useState("");
  const [newpass2, setNewpass2] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ kind: "ok" | "err"; text: string; url?: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.me().then(setMe).catch(() => {});
  }, []);

  const save = async () => {
    setBusy(true);
    setStatus(null);
    try {
      const patch: Parameters<typeof api.updateProfile>[0] = {
        display_name: display || null,
        username: username || null,
        email: email || null,
        phone: phone || null,
      };
      if (curpass || newpass || newpass2) {
        if (!curpass) {
          setStatus({ kind: "err", text: "修改密码需要输入当前密码" });
          setBusy(false);
          return;
        }
        if (newpass !== newpass2) {
          setStatus({ kind: "err", text: "两次输入的新密码不一致" });
          setBusy(false);
          return;
        }
        patch.current_password = curpass;
        patch.new_password = newpass;
      }
      const res = await api.updateProfile(patch);
      const updated = await api.me();
      setMe(updated);
      onUpdated(updated);
      setCurpass("");
      setNewpass("");
      setNewpass2("");
      setStatus({ kind: "ok", text: res.message, url: res.debug_verify_url });
    } catch (err) {
      setStatus({ kind: "err", text: String(err) });
    } finally {
      setBusy(false);
    }
  };

  const uploadAvatar = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setStatus(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      await api.uploadAvatar(fd);
      const updated = await api.me();
      setMe(updated);
      onUpdated(updated);
      setStatus({ kind: "ok", text: "头像已更新" });
    } catch (err) {
      setStatus({ kind: "err", text: String(err) });
    }
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal profile-modal" onClick={(e) => e.stopPropagation()}>
        <button className="modal-close ghost" onClick={onClose}>✖ Close</button>
        <h2 style={{ marginTop: 0 }}>Profile</h2>
        <div className="profile-avatar-row">
          {me.avatar ? (
            <img className="profile-avatar" src={me.avatar} alt="avatar" referrerPolicy="no-referrer" />
          ) : (
            <span className="profile-avatar initial">{(me.display_name || me.username || "?").trim()[0]?.toUpperCase()}</span>
          )}
          <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp,image/gif" onChange={uploadAvatar} />
        </div>
        <label className="field-label">Display name</label>
        <input value={display} onChange={(e) => setDisplay(e.target.value)} />
        <label className="field-label">Username</label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        <label className="field-label">
          Contact email {me.email && !me.email_verified && <span className="error">(未验证)</span>}
        </label>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
        <label className="field-label">Phone</label>
        <input type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} autoComplete="tel" />
        <h3 style={{ margin: "18px 0 0" }}>Change password</h3>
        <label className="field-label">Current password</label>
        <input type="password" value={curpass} onChange={(e) => setCurpass(e.target.value)} autoComplete="current-password" />
        <label className="field-label">New password</label>
        <input type="password" value={newpass} onChange={(e) => setNewpass(e.target.value)} autoComplete="new-password" />
        <label className="field-label">Confirm new password</label>
        <input type="password" value={newpass2} onChange={(e) => setNewpass2(e.target.value)} autoComplete="new-password" />
        {status && (
          <div>
            <p className={status.kind === "ok" ? "success" : "error"}>{status.text}</p>
            {status.url && (
              <div className="debug-link">
                <div className="muted" style={{ fontSize: 12 }}>新邮箱验证链接(SMTP 未配置)</div>
                <input readOnly value={status.url} onFocus={(e) => e.currentTarget.select()} />
              </div>
            )}
          </div>
        )}
        <button className="primary" onClick={save} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
      </div>
    </div>
  );
}

function Sidebar({ page, onNavigate }: { page: Page; onNavigate: (p: Page) => void }) {
  const items: { key: Page; icon: string; label: string }[] = [
    { key: "home", icon: "🏠", label: "Home" },
    { key: "import", icon: "📥", label: "Import Data" },
    { key: "study", icon: "📖", label: "Study Mode" },
    { key: "manage", icon: "🛠️", label: "Manage Vocabulary" },
  ];
  return (
    <aside className="sidebar">
      <div className="sidebar-brand"><img src="/deepdive.png" className="brand-logo" alt="DeepDive" /> DeepDive</div>
      <hr />
      <nav>
        {items.map((it) => (
          <button
            key={it.key}
            className={page === it.key ? "nav active" : "nav"}
            onClick={() => onNavigate(it.key)}
          >
            <span>{it.icon}</span>
            <span>{it.label}</span>
          </button>
        ))}
      </nav>
    </aside>
  );
}

function Home() {
  const [status, setStatus] = useState<"checking" | "ok" | "error">("checking");

  useEffect(() => {
    fetch("/api/health")
      .then((r) => setStatus(r.ok ? "ok" : "error"))
      .catch(() => setStatus("error"));
  }, []);

  return (
    <div>
      <h3 style={{ marginTop: 0, display: "flex", alignItems: "center", gap: 8 }}><img src="/deepdive.png" className="brand-logo brand-logo-lg" alt="DeepDive" /> DeepDive Learning Assistant</h3>
      <h3 style={{ marginTop: 0 }}>Welcome to DeepDive</h3>
      <p className="muted">
        A domain-specific English learning tool tailored for your specific needs.
      </p>
      <p>
        <strong>Please select a function from the sidebar:</strong>
      </p>
      <ul className="muted" style={{ paddingLeft: 20, margin: "8px 0 0" }}>
        <li>📥 <strong>Import Data</strong>: Import your vocabulary, sentences, and build VectorDB index.</li>
        <li>📖 <strong>Study Mode</strong>: Start your immersive and interactive learning session with AI explanations and TTS.</li>
        <li>🛠️ <strong>Manage Vocabulary</strong>: Edit definitions, levels, and enable/disable specific words.</li>
      </ul>

      {status === "checking" && <p className="muted">Checking API environment…</p>}
      {status === "ok" && (
        <p className="success">✅ API Environment is properly configured and ready.</p>
      )}
      {status === "error" && <p className="error">⚠️ API environment is not reachable.</p>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────
// My Account (self-service: profile, balance, daily usage, logs, ledger).
// English, admin-console style; clicking a model name in the usage log
// opens the model detail modal (same shape as the admin modelDetailModal).
// ─────────────────────────────────────────────────────────
const ACCOUNT_PAGE = 20;

function MyAccount({ user }: { user: Me }) {
  const [data, setData] = useState<UsageReport | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [detail, setDetail] = useState<Model | null>(null);

  const load = async (offset: number) => {
    setError("");
    try {
      const [d, m] = await Promise.all([api.usage({ limit: ACCOUNT_PAGE, offset }), api.models()]);
      setData(d);
      setModels(m.models);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => {
    load(0);
  }, []);

  const fmtMoney = (n: number | null | undefined) =>
    n == null ? "—" : "$" + n.toFixed(6).replace(/\.?0+$/, "");
  const fmtDate = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");
  const totalPages = data ? Math.max(1, Math.ceil(data.total / ACCOUNT_PAGE)) : 1;
  const openModel = (name: string) => {
    const m = models.find((x) => x.name === name);
    if (m) setDetail(m);
  };

  if (error) {
    return (
      <div>
        <h3 style={{ marginTop: 0 }}>👤 My Account</h3>
        <p className="error">{error}</p>
      </div>
    );
  }
  if (!data) return <p className="muted">Loading…</p>;

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>👤 My Account</h3>

      {/* Overview */}
      <div className="panel" style={{ display: "flex", gap: 48, flexWrap: "wrap" }}>
        <div>
          <p className="muted" style={{ margin: "4px 0 0" }}>Wallet balance</p>
          <p style={{ margin: 0, fontSize: 28, fontWeight: 700 }}>{fmtMoney(data.balance)} {data.currency}</p>
        </div>
        <div>
          <p className="muted" style={{ margin: "4px 0 0" }}>Usage records</p>
          <p style={{ margin: 0, fontSize: 28, fontWeight: 700 }}>{data.total.toLocaleString()}</p>
        </div>
      </div>

      {/* Profile */}
      <div className="panel" style={{ display: "flex", gap: 20, alignItems: "center" }}>
        <div className="profile-avatar-row" style={{ margin: 0 }}>
          {user.avatar ? (
            <img className="profile-avatar" src={user.avatar} alt="avatar" referrerPolicy="no-referrer" />
          ) : (
            <span className="profile-avatar initial">{(user.display_name || user.username || "?").trim()[0]?.toUpperCase()}</span>
          )}
        </div>
        <div>
          <h3 style={{ margin: 0 }}>{user.display_name || user.username}</h3>
          <p className="muted" style={{ margin: "4px 0" }}>
            @{user.username} · {user.email || "—"}
          </p>
          <span className="badge">{user.role_name || user.role_id}</span>
        </div>
      </div>

      {/* Daily usage */}
      <div className="panel">
        <h3>Daily Usage</h3>
        {data.counters.length ? (
          <table className="st-table">
            <thead>
              <tr><th>Date</th><th>Requests</th><th>Tokens</th></tr>
            </thead>
            <tbody>
              {data.counters.map((c) => (
                <tr key={c.period_start}>
                  <td>{c.period_start.slice(0, 10)}</td>
                  <td>{c.request_count}</td>
                  <td>{c.token_count.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No data</p>
        )}
      </div>

      {/* Usage logs */}
      <div className="panel">
        <h3>Usage Logs</h3>
        {data.logs.length ? (
          <table className="st-table">
            <thead>
              <tr><th>Time</th><th>Model</th><th>Channel</th><th>Tool</th><th>Tokens</th><th>Cost</th></tr>
            </thead>
            <tbody>
              {data.logs.map((l) => (
                <tr key={l.id}>
                  <td>{fmtDate(l.created_at)}</td>
                  <td>
                    {l.model_name ? (
                      <button className="linklike" onClick={() => openModel(l.model_name)}>{l.model_name}</button>
                    ) : "—"}
                  </td>
                  <td>{l.credential_name || "—"}</td>
                  <td>{l.tool || "—"}</td>
                  <td>{l.total_tokens.toLocaleString()}</td>
                  <td>{fmtMoney(l.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No records</p>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10 }}>
          <button className="ghost" disabled={page <= 0} onClick={() => { const p = page - 1; setPage(p); load(p * ACCOUNT_PAGE); }}>
            ‹ Prev
          </button>
          <span className="muted">
            Page {page + 1} / {totalPages} · {data.total} total
          </span>
          <button className="ghost" disabled={page + 1 >= totalPages} onClick={() => { const p = page + 1; setPage(p); load(p * ACCOUNT_PAGE); }}>
            Next ›
          </button>
        </div>
      </div>

      {/* Transactions */}
      <div className="panel">
        <h3>Transactions</h3>
        {data.transactions.length ? (
          <table className="st-table">
            <thead>
              <tr><th>Time</th><th>Type</th><th>Amount</th><th>Balance</th><th>Note</th></tr>
            </thead>
            <tbody>
              {data.transactions.map((t) => (
                <tr key={t.id}>
                  <td>{fmtDate(t.created_at)}</td>
                  <td>{t.type}</td>
                  <td>{t.amount < 0 ? "−" : "+"}{fmtMoney(Math.abs(t.amount))}</td>
                  <td>{fmtMoney(t.balance_after)}</td>
                  <td>{t.description || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No records</p>
        )}
      </div>

      {detail && <ModelDetailModal model={detail} onClose={() => setDetail(null)} />}
    </div>
  );
}

// Model catalog detail modal — mirrors the admin console's modelDetailModal.
function ModelDetailModal({ model, onClose }: { model: Model; onClose: () => void }) {
  const fmtMoney = (n: number) => "$" + n.toFixed(6).replace(/\.?0+$/, "");
  const fmtDate = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");
  const row = (k: string, v: React.ReactNode) => (
    <p style={{ display: "flex", justifyContent: "space-between", gap: 24, margin: "6px 0" }}>
      <span className="muted">{k}</span>
      <span style={{ fontWeight: 600, textAlign: "right" }}>{v}</span>
    </p>
  );
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 520 }}>
        <button className="modal-close ghost" onClick={onClose}>✖ Close</button>
        <h2 style={{ marginTop: 0 }}>{model.name}</h2>
        <p className="muted">Model catalog entry</p>
        {row("Display name (Channel model name)", model.name)}
        {row("Provider model name", model.provider_model_name || "—")}
        {row("Description", model.description || "—")}
        {row("Prompt $/1k", fmtMoney(model.prompt_price_per_1k))}
        {row("Completion $/1k", fmtMoney(model.completion_price_per_1k))}
        {row("Status", model.is_active ? "active" : "inactive")}
        {row("Created", fmtDate(model.created_at))}
      </div>
    </div>
  );
}

function useDomains() {
  const [domains, setDomains] = useState<Domain[]>([]);
  const [error, setError] = useState("");
  const load = () => api.listDomains().then(setDomains).catch((e) => setError(String(e)));
  useEffect(() => {
    load();
  }, []);
  return { domains, error, reload: load };
}

function DomainSelect({
  domains,
  value,
  onChange,
}: {
  domains: Domain[];
  value: string;
  onChange: (id: string) => void;
}) {
  if (domains.length === 0) return <p className="muted">No domains. Create one in Import Data.</p>;
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      {domains.map((d) => (
        <option key={d.id} value={d.id}>
          {d.name}
        </option>
      ))}
    </select>
  );
}

// ─────────────────────────────────────────────────────────
// Import Data (4 tabs)
// ─────────────────────────────────────────────────────────

// Parse a CSV/Excel file into { columns, rows } (first row = header).
// Uses SheetJS so both .csv and .xlsx are handled uniformly in the browser.
function parseTableFile(file: File): Promise<{ columns: string[]; rows: string[][] }> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const data = new Uint8Array(reader.result as ArrayBuffer);
        const wb = XLSX.read(data, { type: "array" });
        const ws = wb.Sheets[wb.SheetNames[0]];
        const all = XLSX.utils.sheet_to_json<string[]>(ws, { header: 1, defval: "" });
        const columns = (all[0] ?? []).map((c) => String(c));
        const rows = all.slice(1).map((r) => r.map((c) => String(c)));
        resolve({ columns, rows });
      } catch (e) {
        reject(e);
      }
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(file);
  });
}

// File uploader (dropzone + "Browse files" button).
function FileUploader({
  label,
  accept,
  typesText,
  onFile,
}: {
  label: string;
  accept: string;
  typesText: string;
  onFile: (file: File) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const handleFiles = (files: FileList | null) => {
    const f = files?.[0];
    if (f) onFile(f);
  };
  return (
    <div className="field">
      <label className="field-label">{label}</label>
      <div
        className={"file-dropzone" + (dragOver ? " drag-over" : "")}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          handleFiles(e.dataTransfer.files);
        }}
      >
        <span className="file-dropzone-icon">⬆️</span>
        <p className="file-dropzone-main">Drag and drop file here</p>
        <p className="file-dropzone-limit">Limit 200MB per file • {typesText}</p>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            inputRef.current?.click();
          }}
        >
          Browse files
        </button>
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          onChange={(e) => handleFiles(e.target.files)}
          style={{ display: "none" }}
        />
      </div>
    </div>
  );
}


function ImportData() {
  const [tab, setTab] = useState(0);
  const tabs = [
    "Domain Management",
    "Import Vocabulary",
    "Import Sentences (SQL)",
    "Import VectorDB (Independent)",
    "Articles & Query Repo",
  ];
  return (
    <div>
      <h1 style={{ marginTop: 0 }}>📥 Data Import Center</h1>
      <div className="info">
        <p>
          <strong>👋 Welcome to the Data Management Dashboard!</strong>
        </p>
        <p>Please follow the steps below to manage your learning resources:</p>
        <ol>
          <li><strong>Domain Management</strong>: Create separate topics (e.g., 'Physics', 'Daily Life').</li>
          <li><strong>Import Vocabulary</strong>: Bulk upload terms. Supports Excel/CSV (Columns: Word, Frequency).</li>
          <li><strong>Import Sentences (SQL)</strong>: Add sentences to the database for keyword matching. Supports TXT/Excel/CSV.</li>
          <li><strong>Import VectorDB (Independent)</strong>: Add sentences to VectorDB for semantic search. Supports TXT/Excel/CSV.</li>
        </ol>
      </div>

      <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "14px 0" }} />

      <div className="tabs">
        {tabs.map((t, i) => (
          <button key={t} className={tab === i ? "tab active" : "tab"} onClick={() => setTab(i)}>
            {t}
          </button>
        ))}
      </div>

      {tab === 0 && <DomainTab />}
      {tab === 1 && <VocabularyTab />}
      {tab === 2 && <SentencesTab />}
      {tab === 3 && <VectorTab />}
      {tab === 4 && <ArticlesTab />}
    </div>
  );
}

function DomainTab() {
  const { domains, reload } = useDomains();
  const [name, setName] = useState("");
  const [msg, setMsg] = useState("");

  const create = async () => {
    if (!name.trim()) return;
    setMsg("");
    try {
      await api.createDomain(name.trim());
      setName("");
      setMsg("✅ Domain created.");
      await reload();
    } catch (e) {
      setMsg(String(e));
    }
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Create or View Domains</h3>
      <div className="field">
        <label className="field-label">New Domain Name</label>
        <input
          placeholder="e.g. Stanford_CS336"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && create()}
          style={{ width: "100%" }}
        />
      </div>
      <button onClick={create} disabled={!name.trim()}>
        Create Domain
      </button>
      {msg && <p className="muted">{msg}</p>}

      {domains.length > 0 ? (
        <>
          <p className="muted" style={{ marginBottom: 6 }}>
            Existing Domains:
          </p>
          <table className="st-table">
            <thead>
              <tr>
                <th></th>
                <th>ID</th>
                <th>Name</th>
              </tr>
            </thead>
            <tbody>
              {domains.map((d, i) => (
                <tr key={d.id}>
                  <td>{i}</td>
                  <td>{d.id}</td>
                  <td>{d.name}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p className="muted">No domains found. Please create one.</p>
      )}
    </div>
  );
}

function VocabularyTab() {
  const { domains } = useDomains();
  const [domainId, setDomainId] = useState("");
  const [mode, setMode] = useState<"file" | "manual">("file");
  const [text, setText] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");

  const [columns, setColumns] = useState<string[]>([]);
  const [rows, setRows] = useState<string[][]>([]);
  const [wordCol, setWordCol] = useState("");
  const [freqCol, setFreqCol] = useState("");

  const activeDomain = domainId || domains[0]?.id || "";

  const doImport = async (items: { word: string; frequency: number }[]): Promise<boolean> => {
    if (!items.length || !activeDomain) return false;
    setError("");
    setResult("");
    try {
      const r = await api.importTermsStructured(
        activeDomain,
        items.map((it) => ({ word: it.word, frequency: it.frequency }))
      );
      setResult(`✅ Imported ${r.added} terms, skipped ${r.skipped}.`);
      return true;
    } catch (e) {
      setError(String(e));
      return false;
    }
  };

  const onFile = async (file: File) => {
    setError("");
    setResult("");
    try {
      const parsed = await parseTableFile(file);
      setColumns(parsed.columns);
      setRows(parsed.rows);
      setWordCol(parsed.columns[0] ?? "");
      setFreqCol("");
    } catch (e) {
      setError(String(e));
    }
  };

  const importFile = async () => {
    const wi = columns.indexOf(wordCol);
    if (wi < 0) {
      setError("Please select the Word column.");
      return;
    }
    const fi = freqCol ? columns.indexOf(freqCol) : -1;
    const items: { word: string; frequency: number }[] = [];
    for (const row of rows) {
      const word = (row[wi] ?? "").trim();
      if (!word || word.toLowerCase() === "nan") continue;
      let frequency = 1;
      if (fi >= 0) {
        const n = parseInt((row[fi] ?? "").trim(), 10);
        if (!isNaN(n)) frequency = n;
      }
      items.push({ word, frequency });
    }
    await doImport(items);
  };

  const importManual = async () => {
    const items: { word: string; frequency: number }[] = [];
    for (const line of text.split("\n")) {
      const m = line.match(/^(.*?)\s+(\d+)\s*$/);
      const word = (m ? m[1] : line).trim();
      if (!word) continue;
      const frequency = m ? parseInt(m[2], 10) : 1;
      items.push({ word, frequency });
    }
    if (await doImport(items)) setText("");
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Import Vocabulary</h3>
      <div className="field">
        <label className="field-label">Target Domain:</label>
        <DomainSelect domains={domains} value={activeDomain} onChange={setDomainId} />
      </div>

      <div className="tabs">
        <button className={mode === "file" ? "tab active" : "tab"} onClick={() => setMode("file")}>
          📂 File Upload (Excel/CSV)
        </button>
        <button className={mode === "manual" ? "tab active" : "tab"} onClick={() => setMode("manual")}>
          ✍️ Manual Input
        </button>
      </div>

      {mode === "file" ? (
        <div>
          <p className="muted">Upload a file with columns for Word and optional Frequency.</p>
          <FileUploader
            label="Upload Excel/CSV"
            accept=".csv,.xlsx,.xls"
            typesText="CSV, XLSX"
            onFile={onFile}
          />
          {columns.length > 0 && (
            <>
              {rows.length > 0 && (
                <table className="table" style={{ marginTop: 10 }}>
                  <thead>
                    <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {rows.slice(0, 3).map((r, i) => (
                      <tr key={i}>{columns.map((_, ci) => <td key={ci}>{r[ci]}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="row" style={{ marginTop: 10 }}>
                <span className="muted">Word Column:</span>
                <select value={wordCol} onChange={(e) => setWordCol(e.target.value)}>
                  {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
              <div className="row" style={{ marginTop: 8 }}>
                <span className="muted">Frequency Column (optional):</span>
                <select value={freqCol} onChange={(e) => setFreqCol(e.target.value)}>
                  <option value="">-- None --</option>
                  {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
              <button
                className="primary"
                onClick={importFile}
                disabled={!wordCol || !activeDomain}
                style={{ marginTop: 10 }}
              >
                🚀 Import Vocabulary
              </button>
            </>
          )}
        </div>
      ) : (
        <div>
          <p className="muted">
            Enter one word per line. Format: <code>Word</code> or <code>Word Frequency</code> (e.g. 'Apple 5').
          </p>
          <div className="field">
            <label className="field-label">Paste Words Here</label>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              style={{ minHeight: 140 }}
            />
          </div>
          <button
            onClick={importManual}
            disabled={!text.trim() || !activeDomain}
            style={{ marginTop: 8 }}
          >
            📥 Import Text
          </button>
        </div>
      )}

      {result && <p className="muted">{result}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function SentencesTab() {
  const { domains } = useDomains();
  const [domainId, setDomainId] = useState("");
  const [mode, setMode] = useState<"file" | "manual">("file");
  const [text, setText] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");

  const [columns, setColumns] = useState<string[]>([]);
  const [rows, setRows] = useState<string[][]>([]);
  const [sentCol, setSentCol] = useState("");
  const [txtContent, setTxtContent] = useState("");

  const activeDomain = domainId || domains[0]?.id || "";

  // ── Query repository: push saved sentences into the unified RAG search repo ──
  const [sentences, setSentences] = useState<Sentence[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const repoJob = useJob<{ chunks: number }>();
  const [repoMsg, setRepoMsg] = useState("");
  const [repoErr, setRepoErr] = useState("");

  useEffect(() => {
    if (!activeDomain) {
      setSentences([]);
      setSelected(new Set());
      return;
    }
    let cancelled = false;
    api
      .listSentences(activeDomain)
      .then((rows) => {
        if (!cancelled) setSentences(rows);
      })
      .catch(() => {
        if (!cancelled) setSentences([]);
      });
    return () => {
      cancelled = true;
    };
  }, [activeDomain]);

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const importToRepo = async () => {
    if (!selected.size) return;
    setRepoMsg("");
    setRepoErr("");
    try {
      const r = await repoJob.run(() => api.learningImport("sentence", [...selected]));
      setRepoMsg(`✅ Imported ${r.chunks} chunks into the query repository.`);
      setSelected(new Set());
    } catch (e) {
      setRepoErr(String(e));
    }
  };

  const splitLines = (raw: string): string[] =>
    raw.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.length > 5);

  const doImport = async (sentences: string[]): Promise<boolean> => {
    if (!sentences.length || !activeDomain) return false;
    setError("");
    setResult("");
    try {
      const r = await api.importSentencesStructured(activeDomain, sentences);
      setResult(`✅ Saved ${r.added} sentences to SQL, skipped ${r.skipped}.`);
      return true;
    } catch (e) {
      setError(String(e));
      return false;
    }
  };

  const onFile = async (file: File) => {
    setError("");
    setResult("");
    setColumns([]);
    setRows([]);
    setSentCol("");
    setTxtContent("");
    if (file.name.toLowerCase().endsWith(".txt")) {
      setTxtContent(await file.text());
    } else {
      try {
        const parsed = await parseTableFile(file);
        setColumns(parsed.columns);
        setRows(parsed.rows);
        setSentCol(parsed.columns[0] ?? "");
      } catch (e) {
        setError(String(e));
      }
    }
  };

  const importTxt = async () => {
    if (await doImport(splitLines(txtContent))) setTxtContent("");
  };

  const importFile = async () => {
    const si = columns.indexOf(sentCol);
    if (si < 0) {
      setError("Please select the Sentence column.");
      return;
    }
    const sentences = rows.map((r) => (r[si] ?? "").trim()).filter((s) => s.length > 5);
    await doImport(sentences);
  };

  const importManual = async () => {
    if (await doImport(splitLines(text))) setText("");
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Import to Sentence Corpus</h3>
      <p className="muted">Sentences imported here are stored in SQL for exact keyword matching.</p>
      <div className="field">
        <label className="field-label">Target Domain:</label>
        <DomainSelect domains={domains} value={activeDomain} onChange={setDomainId} />
      </div>

      <div className="tabs">
        <button className={mode === "file" ? "tab active" : "tab"} onClick={() => setMode("file")}>
          📂 File Upload (TXT/Excel)
        </button>
        <button className={mode === "manual" ? "tab active" : "tab"} onClick={() => setMode("manual")}>
          ✍️ Manual Input
        </button>
      </div>

      {mode === "file" ? (
        <div>
          <p className="muted">Upload TXT (one sentence per line) or Excel/CSV (select a sentence column).</p>
          <FileUploader
            label="Upload File"
            accept=".txt,.csv,.xlsx,.xls"
            typesText="TXT, CSV, XLSX"
            onFile={onFile}
          />
          {txtContent && (
            <>
              <pre className="markdown" style={{ marginTop: 8, maxHeight: 120, overflow: "auto" }}>
                {txtContent.slice(0, 500)}
              </pre>
              <button
                onClick={importTxt}
                disabled={!txtContent.trim() || !activeDomain}
                style={{ marginTop: 8 }}
              >
                📥 Import TXT
              </button>
            </>
          )}
          {columns.length > 0 && (
            <>
              {rows.length > 0 && (
                <table className="table" style={{ marginTop: 10 }}>
                  <thead>
                    <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {rows.slice(0, 3).map((r, i) => (
                      <tr key={i}>{columns.map((_, ci) => <td key={ci}>{r[ci]}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="row" style={{ marginTop: 10 }}>
                <span className="muted">Sentence Column:</span>
                <select value={sentCol} onChange={(e) => setSentCol(e.target.value)}>
                  {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
              <button
                onClick={importFile}
                disabled={!sentCol || !activeDomain}
                style={{ marginTop: 10 }}
              >
                📥 Import Table Data
              </button>
            </>
          )}
        </div>
      ) : (
        <div>
          <div className="field">
            <label className="field-label">Paste Sentences (one per line)</label>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              style={{ minHeight: 140 }}
            />
          </div>
          <button
            onClick={importManual}
            disabled={!text.trim() || !activeDomain}
            style={{ marginTop: 8 }}
          >
            💾 Save to Database
          </button>
        </div>
      )}

      <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "14px 0" }} />
      <h4 style={{ margin: 0 }}>Query Repository</h4>
      <p className="muted">
        Push saved sentences into the unified RAG search repo (source_type='learning').
      </p>
      <button
        onClick={importToRepo}
        disabled={!selected.size || repoJob.busy}
        style={{ marginBottom: 8 }}
      >
        {repoJob.busy
          ? "Importing…"
          : `📥 Import ${selected.size} to Query Repo`}
      </button>
      {repoMsg && <p className="muted">{repoMsg}</p>}
      {repoErr && <p className="error">{repoErr}</p>}
      {sentences.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th></th>
              <th>Sentence</th>
            </tr>
          </thead>
          <tbody>
            {sentences.slice(0, 100).map((s) => (
              <tr key={s.id}>
                <td>
                  <input
                    type="checkbox"
                    checked={selected.has(s.id)}
                    onChange={() => toggleSelect(s.id)}
                  />
                </td>
                <td>{s.content_en}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">No sentences in the active domain yet.</p>
      )}

      {result && <p className="muted">{result}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function ArticlesTab() {
  const { domains } = useDomains();
  const [articles, setArticles] = useState<Article[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [domainId, setDomainId] = useState("");
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [importingId, setImportingId] = useState<string | null>(null);

  const reload = async () => {
    try {
      const r = await api.listArticles();
      setArticles(r.items);
    } catch (e) {
      setError(String(e));
    }
  };
  useEffect(() => {
    reload();
  }, []);

  const save = async (alsoImport: boolean) => {
    setMsg("");
    setError("");
    try {
      const a = await api.createArticle({
        title: title.trim(),
        content,
        domain_id: domainId || null,
      });
      setTitle("");
      setContent("");
      setMsg(`✅ Article "${a.title}" saved.`);
      await reload();
      if (alsoImport) {
        const r = await api.importArticleToRepo(a.id);
        setMsg(`✅ Article saved & imported ${r.chunks} chunks into the query repository.`);
      }
    } catch (e) {
      setError(String(e));
    }
  };

  const remove = async (id: string) => {
    setError("");
    try {
      await api.deleteArticle(id);
      await reload();
    } catch (e) {
      setError(String(e));
    }
  };

  const importOne = async (id: string) => {
    setError("");
    setImportingId(id);
    try {
      const r = await api.importArticleToRepo(id);
      setMsg(`✅ Imported ${r.chunks} chunks into the query repository.`);
    } catch (e) {
      setError(String(e));
    } finally {
      setImportingId(null);
    }
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Articles (Query Repository)</h3>
      <p className="muted">Write study articles; import them into the unified RAG search repo.</p>
      <div className="field">
        <label className="field-label">Title</label>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="e.g. The Water Cycle"
        />
      </div>
      <div className="field">
        <label className="field-label">Content</label>
        <textarea value={content} onChange={(e) => setContent(e.target.value)} style={{ minHeight: 160 }} />
      </div>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="muted">Domain (optional):</span>
        <DomainSelect domains={domains} value={domainId} onChange={setDomainId} />
      </div>
      <button onClick={() => save(false)} disabled={!title.trim() || !content.trim()}>
        💾 Save Article
      </button>{" "}
      <button
        onClick={() => save(true)}
        disabled={!title.trim() || !content.trim() || importingId !== null}
      >
        💾 Save & Import to Query Repo
      </button>
      {msg && <p className="muted">{msg}</p>}
      {error && <p className="error">{error}</p>}

      {articles.length > 0 ? (
        <table className="table" style={{ marginTop: 12 }}>
          <thead>
            <tr>
              <th>Title</th>
              <th>Preview</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {articles.map((a) => (
              <tr key={a.id}>
                <td>{a.title}</td>
                <td className="muted">{a.content.slice(0, 80)}…</td>
                <td className="muted">{new Date(a.created_at).toLocaleString()}</td>
                <td>
                  <button onClick={() => importOne(a.id)} disabled={importingId !== null}>
                    {importingId === a.id ? "Importing…" : "Import to Query Repo"}
                  </button>{" "}
                  <button onClick={() => remove(a.id)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted" style={{ marginTop: 12 }}>No articles yet.</p>
      )}
    </div>
  );
}

function VectorTab() {
  const { domains } = useDomains();
  const [domainId, setDomainId] = useState("");
  const [mode, setMode] = useState<"file" | "manual" | "test">("manual");
  const [text, setText] = useState("");
  const [columns, setColumns] = useState<string[]>([]);
  const [rows, setRows] = useState<string[][]>([]);
  const [sentCol, setSentCol] = useState("");
  const [txtContent, setTxtContent] = useState("");
  const [indexMsg, setIndexMsg] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Sentence[]>([]);
  const [error, setError] = useState("");

  const activeDomain = domainId || domains[0]?.id || "";
  const indexJob = useJob<{ indexed: number }>();

  const splitLines = (raw: string): string[] =>
    raw.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.length > 5);

  const importAndIndex = async (sentences: string[]): Promise<boolean> => {
    if (!sentences.length || !activeDomain) return false;
    setError("");
    setIndexMsg("Importing & building embeddings… (first run downloads the embedding model)");
    try {
      const r = await api.importSentencesStructured(activeDomain, sentences);
      setIndexMsg(`Imported ${r.added} sentences. Indexing in the background…`);
      const idx = await indexJob.run(() => api.enqueueIndexSentences(activeDomain));
      setIndexMsg(`✅ Imported ${r.added} & indexed ${idx.indexed} sentences.`);
      return true;
    } catch (e) {
      setError(String(e));
      setIndexMsg("");
      return false;
    }
  };

  const onFile = async (file: File) => {
    setError("");
    setIndexMsg("");
    setColumns([]);
    setRows([]);
    setSentCol("");
    setTxtContent("");
    if (file.name.toLowerCase().endsWith(".txt")) {
      setTxtContent(await file.text());
    } else {
      try {
        const parsed = await parseTableFile(file);
        setColumns(parsed.columns);
        setRows(parsed.rows);
        setSentCol(parsed.columns[0] ?? "");
      } catch (e) {
        setError(String(e));
      }
    }
  };

  const importTxt = async () => {
    if (await importAndIndex(splitLines(txtContent))) setTxtContent("");
  };

  const importFile = async () => {
    const si = columns.indexOf(sentCol);
    if (si < 0) {
      setError("Please select the Sentence column.");
      return;
    }
    await importAndIndex(rows.map((r) => (r[si] ?? "").trim()).filter((s) => s.length > 5));
  };

  const importManual = async () => {
    if (await importAndIndex(splitLines(text))) setText("");
  };

  const testSearch = async () => {
    if (!query.trim() || !activeDomain) return;
    setError("");
    try {
      setResults(await api.semanticSearch(activeDomain, query.trim()));
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Direct Import to Vector Database</h3>
      <div className="note">
        <strong>Note:</strong> Data here is stored <strong>independently</strong> in the AI Vector
        Database (pgvector). It is NOT synced with the sentence corpus. Use this for semantic search when exact
        matches fail.
      </div>
      <div className="field">
        <label className="field-label">Target Domain:</label>
        <DomainSelect domains={domains} value={activeDomain} onChange={setDomainId} />
      </div>

      <div className="tabs">
        <button className={mode === "file" ? "tab active" : "tab"} onClick={() => setMode("file")}>
          📂 File Upload (TXT/Excel)
        </button>
        <button className={mode === "manual" ? "tab active" : "tab"} onClick={() => setMode("manual")}>
          ✍️ Manual Input
        </button>
        <button className={mode === "test" ? "tab active" : "tab"} onClick={() => setMode("test")}>
          🧪 Test Search
        </button>
      </div>

      {mode === "file" && (
        <div>
          <FileUploader
            label="Upload Corpus for AI Indexing"
            accept=".txt,.csv,.xlsx,.xls"
            typesText="TXT, CSV, XLSX"
            onFile={onFile}
          />
          {txtContent && (
            <>
              <pre className="markdown" style={{ marginTop: 8, maxHeight: 120, overflow: "auto" }}>
                {txtContent.slice(0, 500)}
              </pre>
              <button
                className="primary"
                onClick={importTxt}
                disabled={!txtContent.trim() || !activeDomain}
                style={{ marginTop: 8 }}
              >
                🧠 Build Index (TXT)
              </button>
            </>
          )}
          {columns.length > 0 && (
            <>
              {rows.length > 0 && (
                <table className="table" style={{ marginTop: 10 }}>
                  <thead>
                    <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {rows.slice(0, 3).map((r, i) => (
                      <tr key={i}>{columns.map((_, ci) => <td key={ci}>{r[ci]}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="row" style={{ marginTop: 10 }}>
                <span className="muted">Sentence Column:</span>
                <select value={sentCol} onChange={(e) => setSentCol(e.target.value)}>
                  {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
              <button
                className="primary"
                onClick={importFile}
                disabled={!sentCol || !activeDomain}
                style={{ marginTop: 10 }}
              >
                🧠 Build Index (Table)
              </button>
            </>
          )}
        </div>
      )}

      {mode === "manual" && (
        <div>
          <div className="field">
            <label className="field-label">Paste raw text / sentences</label>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              style={{ minHeight: 140 }}
            />
          </div>
          <button
            className="primary"
            onClick={importManual}
            disabled={!text.trim() || !activeDomain}
            style={{ marginTop: 8 }}
          >
            🧠 Build Independent Vector Index
          </button>
        </div>
      )}

      {mode === "test" && (
        <div>
          <h3 style={{ marginTop: 0 }}>🧪 Test Semantic Search</h3>
          <p className="muted">Verify your VectorDB data by searching for similar sentences (fuzzy matching).</p>
          <div className="field">
            <label className="field-label">Enter a query sentence/phrase:</label>
            <input
              placeholder="e.g., neural network architecture"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && testSearch()}
              style={{ width: "100%" }}
            />
          </div>
          <button onClick={testSearch} disabled={!query.trim() || !activeDomain}>
            🔎 Search in VectorDB
          </button>

          {results.length > 0 && (
            <div className="list" style={{ marginTop: 10 }}>
              {results.map((s, i) => (
                <div key={s.id} className="list-item">
                  <span>
                    {i + 1}. {s.content_en}
                    {s.score !== undefined && (
                      <span className="badge" style={{ marginLeft: 8 }}>
                        {s.score}
                      </span>
                    )}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {indexMsg && <p className="muted" style={{ marginTop: 8 }}>{indexMsg}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────
// Study Mode (Vocabulary List + Deep Dive)
// ─────────────────────────────────────────────────────────
type SortKey = "word" | "frequency" | "star_level";

function StudyMode() {
  const { domains } = useDomains();
  const [domainId, setDomainId] = useState("");
  const [terms, setTerms] = useState<Term[]>([]);
  const [query, setQuery] = useState("");
  const [starFilter, setStarFilter] = useState(0);
  const [sort, setSort] = useState<SortKey>("frequency");
  const [sortAsc, setSortAsc] = useState(false);
  const [page, setPage] = useState(1);
  const [activeTerm, setActiveTerm] = useState<Term | null>(null);
  const [error, setError] = useState("");

  const activeDomain = domainId || domains[0]?.id || "";

  useEffect(() => {
    if (!activeDomain) return;
    setPage(1);
    api.listTerms(activeDomain).then(setTerms).catch((e) => setError(String(e)));
  }, [activeDomain]);

  const handleSort = (key: SortKey) => {
    if (sort === key) setSortAsc((v) => !v);
    else {
      setSort(key);
      setSortAsc(key === "word");
    }
    setPage(1);
  };

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    let filtered = q ? terms.filter((t) => t.word.toLowerCase().includes(q)) : terms;
    if (starFilter > 0) filtered = filtered.filter((t) => t.star_level === starFilter);
    const sorted = [...filtered];
    if (sort === "word") sorted.sort((a, b) => a.word.localeCompare(b.word));
    else if (sort === "frequency") sorted.sort((a, b) => a.frequency - b.frequency);
    else sorted.sort((a, b) => a.star_level - b.star_level);
    if (!sortAsc) sorted.reverse();
    return sorted;
  }, [terms, query, sort, sortAsc, starFilter]);

  const totalItems = visible.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageItems = visible.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const arrow = (key: SortKey) => (sort === key ? (sortAsc ? " 🔼" : " 🔽") : "");

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Vocabulary List</h2>

      <div className="filter-row">
        <DomainSelect domains={domains} value={activeDomain} onChange={(id) => setDomainId(id)} />
        <select value={starFilter} onChange={(e) => { setStarFilter(Number(e.target.value)); setPage(1); }}>
          <option value={0}>All Levels</option>
          {[1, 2, 3, 4, 5].map((i) => (
            <option key={i} value={i}>
              {"⭐".repeat(i)} {i} Star{i > 1 ? "s" : ""}
            </option>
          ))}
        </select>
      </div>

      <div className="row grow" style={{ marginBottom: 12 }}>
        <input
          placeholder="🔍 Search for a term..."
          value={query}
          onChange={(e) => { setQuery(e.target.value); setPage(1); }}
        />
      </div>

      {pageItems.length === 0 ? (
        <p className="muted">No vocabulary found in this domain.</p>
      ) : (
        <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
          <table className="table">
            <thead>
              <tr>
                <th><button className="th-sort" onClick={() => handleSort("word")}>WORD{arrow("word")}</button></th>
                <th><button className="th-sort" onClick={() => handleSort("frequency")}>FREQUENCY{arrow("frequency")}</button></th>
                <th><button className="th-sort" onClick={() => handleSort("star_level")}>LEVEL{arrow("star_level")}</button></th>
                <th>DEFINITION</th>
                <th>ACTION</th>
              </tr>
            </thead>
            <tbody>
              {pageItems.map((t) => (
                <tr key={t.id}>
                  <td>
                    <a className="term-link" onClick={() => setActiveTerm(t)}>{t.word}</a>
                  </td>
                  <td className="muted">🔄 {t.frequency}</td>
                  <td>{"⭐".repeat(t.star_level)}</td>
                  <td><ViewDef word={t.word} definition={t.definition} /></td>
                  <td>
                    <button className="primary" onClick={() => setActiveTerm(t)}>🤿 Deep Dive</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {totalItems > 0 && (
        <div className="pagination">
          <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={safePage <= 1}>
            ⬅️ Prev
          </button>
          <span className="muted">
            Page <b>{safePage}</b> of <b>{totalPages}</b> | {totalItems} terms
          </span>
          <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={safePage >= totalPages}>
            Next ➡️
          </button>
        </div>
      )}

      {error && <p className="error">{error}</p>}

      {activeTerm && (
        <div className="modal-overlay" onClick={() => setActiveTerm(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <button className="modal-close ghost" onClick={() => setActiveTerm(null)}>✖ Close</button>
            <TermDetail
              key={activeTerm.id}
              domain={{ id: activeDomain, name: domains.find((d) => d.id === activeDomain)?.name ?? "" }}
              term={activeTerm}
              onNavigate={setActiveTerm}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function ViewDef({ word, definition }: { word: string; definition?: string | null }) {
  const [show, setShow] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!show) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setShow(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [show]);

  if (!definition) return <span className="muted">—</span>;
  return (
    <span className="hover-wrap" ref={wrapRef}>
      <button className="view-btn" onClick={() => setShow((v) => !v)}>📖 View</button>
      {show && (
        <span className="popover">
          <strong>{word}</strong>
          <span className="popover-def">{definition}</span>
        </span>
      )}
    </span>
  );
}

// ─────────────────────────────────────────────────────────
// Manage Vocabulary
// ─────────────────────────────────────────────────────────
function ManageVocabulary() {
  const { domains } = useDomains();
  const [domainId, setDomainId] = useState("");
  const [terms, setTerms] = useState<Term[]>([]);
  const [drafts, setDrafts] = useState<Record<string, Partial<Term>>>({});
  const [query, setQuery] = useState("");
  const [starFilter, setStarFilter] = useState(0);
  const [sort, setSort] = useState<SortKey>("star_level");
  const [sortAsc, setSortAsc] = useState(false);
  const [page, setPage] = useState(1);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  const activeDomain = domainId || domains[0]?.id || "";

  useEffect(() => {
    if (!activeDomain) return;
    setPage(1);
    api.listTerms(activeDomain).then(setTerms).catch((e) => setError(String(e)));
  }, [activeDomain]);

  const handleSort = (key: SortKey) => {
    if (sort === key) setSortAsc((v) => !v);
    else {
      setSort(key);
      setSortAsc(key === "word");
    }
    setPage(1);
  };

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    let filtered = q ? terms.filter((t) => t.word.toLowerCase().includes(q)) : terms;
    if (starFilter > 0) filtered = filtered.filter((t) => t.star_level === starFilter);
    const sorted = [...filtered];
    if (sort === "word") sorted.sort((a, b) => a.word.localeCompare(b.word));
    else if (sort === "frequency") sorted.sort((a, b) => a.frequency - b.frequency);
    else sorted.sort((a, b) => a.star_level - b.star_level);
    if (!sortAsc) sorted.reverse();
    return sorted;
  }, [terms, query, sort, sortAsc, starFilter]);

  const totalItems = visible.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageItems = visible.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const setDraft = (id: string, patch: Partial<Term>) =>
    setDrafts((d) => ({ ...d, [id]: { ...d[id], ...patch } }));

  const draftValue = (t: Term, key: keyof Term, fallback: Term[keyof Term]) =>
    drafts[t.id]?.[key] ?? fallback;

  const saveCurrentPage = async () => {
    const updates = pageItems
      .filter((t) => drafts[t.id] && Object.keys(drafts[t.id]).length > 0)
      .map((t) => ({ term_id: t.id, ...drafts[t.id] }));
    if (updates.length === 0) return;
    setError("");
    setSaved("");
    try {
      await api.bulkUpdateTerms(updates);
      setDrafts({});
      setSaved("✅ Changes saved successfully!");
      setTerms(await api.listTerms(activeDomain));
    } catch (e) {
      setError(String(e));
    }
  };

  const arrow = (key: SortKey) => (sort === key ? (sortAsc ? " 🔼" : " 🔽") : "");

  return (
    <div>
      <h1 style={{ marginTop: 0 }}>🛠️ Manage Vocabulary</h1>

      <div className="filter-row">
        <DomainSelect domains={domains} value={activeDomain} onChange={(id) => setDomainId(id)} />
        <select value={starFilter} onChange={(e) => { setStarFilter(Number(e.target.value)); setPage(1); }}>
          <option value={0}>All Levels</option>
          {[1, 2, 3, 4, 5].map((i) => (
            <option key={i} value={i}>
              {"⭐".repeat(i)} {i} Star{i > 1 ? "s" : ""}
            </option>
          ))}
        </select>
      </div>

      <div className="row grow" style={{ marginBottom: 12 }}>
        <input
          placeholder="🔍 Search for a term to edit..."
          value={query}
          onChange={(e) => { setQuery(e.target.value); setPage(1); }}
        />
      </div>

      {pageItems.length === 0 ? (
        <p className="muted">No vocabulary found in this domain.</p>
      ) : (
        <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
          <table className="table">
            <thead>
              <tr>
                <th>ENABLE</th>
                <th><button className="th-sort" onClick={() => handleSort("word")}>WORD{arrow("word")}</button></th>
                <th><button className="th-sort" onClick={() => handleSort("frequency")}>FREQ{arrow("frequency")}</button></th>
                <th><button className="th-sort" onClick={() => handleSort("star_level")}>LEVEL{arrow("star_level")}</button></th>
                <th>DEFINITION (CLICK TO EDIT)</th>
              </tr>
            </thead>
            <tbody>
              {pageItems.map((t) => {
                const word = String(draftValue(t, "word", t.word));
                const active = Boolean(draftValue(t, "is_active", t.is_active));
                const level = Number(draftValue(t, "star_level", t.star_level));
                const definition = String(draftValue(t, "definition", t.definition ?? ""));
                return (
                  <tr key={t.id}>
                    <td>
                      <label className="switch">
                        <input
                          type="checkbox"
                          checked={active}
                          onChange={(e) => setDraft(t.id, { is_active: e.target.checked })}
                        />
                        <span className="slider" />
                      </label>
                    </td>
                    <td>
                      <input
                        value={word}
                        onChange={(e) => setDraft(t.id, { word: e.target.value })}
                        style={{ width: "100%" }}
                      />
                    </td>
                    <td className="muted">🔄 {t.frequency}</td>
                    <td>
                      <select value={level} onChange={(e) => setDraft(t.id, { star_level: Number(e.target.value) })}>
                        {[1, 2, 3, 4, 5].map((i) => (
                          <option key={i} value={i}>
                            {"⭐".repeat(i)}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      {editingId === t.id ? (
                        <input
                          autoFocus
                          value={definition}
                          onChange={(e) => setDraft(t.id, { definition: e.target.value })}
                          onBlur={() => setEditingId(null)}
                          onKeyDown={(e) => e.key === "Enter" && setEditingId(null)}
                          style={{ width: "100%" }}
                        />
                      ) : (
                        <span
                          className="def-editable"
                          title="Click to edit"
                          onClick={() => setEditingId(t.id)}
                        >
                          {definition || "—"}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {pageItems.length > 0 && (
        <div className="row" style={{ justifyContent: "center", marginTop: 12 }}>
          <button className="primary" onClick={saveCurrentPage}>💾 Save Current Page</button>
        </div>
      )}

      {totalItems > 0 && (
        <div className="pagination">
          <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={safePage <= 1}>
            ⬅️ Prev
          </button>
          <span className="muted">
            Page <b>{safePage}</b> of <b>{totalPages}</b> | Total: {totalItems} terms
          </span>
          <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={safePage >= totalPages}>
            Next ➡️
          </button>
        </div>
      )}

      {saved && <p className="muted">{saved}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Stars({
  value,
  readonly = false,
  onChange,
}: {
  value: number;
  readonly?: boolean;
  onChange?: (v: number) => void;
}) {
  return (
    <span
      className="stars"
      onClick={(e) => {
        if (readonly) return;
        const target = e.target as HTMLElement;
        const idx = Number(target.dataset.idx);
        if (idx) onChange?.(idx);
      }}
    >
      {[1, 2, 3, 4, 5].map((i) => (
        <span key={i} data-idx={i} className={i <= value ? "on" : ""}>
          ★
        </span>
      ))}
    </span>
  );
}

function SentenceCard({ term, sentence }: { term: Term; sentence: Sentence }) {
  const [translation, setTranslation] = useState(sentence.content_cn ?? "");
  const [audio, setAudio] = useState<string>(sentence.audio_hash ?? "");
  const [explanation, setExplanation] = useState<string>(sentence.cn_explanation ?? "");
  const [syntax, setSyntax] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");

  const audioJob = useJob<{ url: string }>();
  const explainJob = useJob<{ translation: string; explanation: string }>();
  const syntaxJob = useJob<{ analysis: string }>();

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    setError("");
    setMsg("");
    try {
      await fn();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  };

  const genAudio = () =>
    run("audio", async () => {
      const { url } = await audioJob.run(() => api.enqueueTts(sentence.content_en));
      setAudio(url);
      await api.updateSentence(sentence.id, { audio_hash: url });
      setMsg("✅ Pronunciation saved.");
    });

  const aiExplain = () =>
    run("explain", async () => {
      const res = await explainJob.run(() => api.enqueueExplain(term.word, sentence.content_en));
      setTranslation(res.translation);
      setExplanation(res.explanation);
      await api.updateSentence(sentence.id, { content_cn: res.translation });
      await api.linkTermToSentence(term.id, sentence.id, res.explanation);
      setMsg("✅ Translation + explanation saved.");
    });

  const analyze = () =>
    run("syntax", async () => {
      const { analysis } = await syntaxJob.run(() => api.enqueueAnalyzeSyntax(sentence.content_en));
      setSyntax(analysis);
    });

  const saveTranslation = () =>
    run("save", async () => {
      await api.updateSentence(sentence.id, { content_cn: translation });
      setMsg("✅ Translation saved.");
    });

  return (
    <div className="panel" style={{ marginTop: 8 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="badge">✓ Linked Match</span>
      </div>
      <p style={{ fontWeight: 600, margin: "8px 0 4px" }}>{sentence.content_en}</p>

      <div className="row" style={{ marginBottom: 8 }}>
        {audio ? (
          <audio controls src={audio} style={{ flex: 1, height: 32 }} />
        ) : (
          <span className="muted" style={{ fontSize: 13 }}>🔇 No audio available</span>
        )}
        <button onClick={genAudio} disabled={busy === "audio"}>
          {busy === "audio" ? "Generating…" : "✨ Gen Pronunciation"}
        </button>
      </div>

      <div style={{ background: "#f9fafb", borderRadius: 8, padding: "10px 12px", marginBottom: 8 }}>
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
          <strong style={{ fontSize: 13 }}>🎙️ Record &amp; Compare</strong>
        </div>
        <MicRecorder />
      </div>

      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <strong style={{ fontSize: 13 }}>Translation (中文)</strong>
        <button onClick={saveTranslation} disabled={busy === "save"}>💾 Save</button>
      </div>
      <textarea
        value={translation}
        onChange={(e) => setTranslation(e.target.value)}
        placeholder="Chinese translation…"
        style={{ minHeight: 60 }}
      />
      {explanation && <p className="muted" style={{ marginTop: 6, fontSize: 13 }}>💡 {explanation}</p>}

      <div className="row" style={{ marginTop: 8 }}>
        <button className="primary" onClick={aiExplain} disabled={busy === "explain"}>
          {busy === "explain" ? "Explaining…" : "✨ AI Explain"}
        </button>
        <button onClick={analyze} disabled={busy === "syntax"}>
          {busy === "syntax" ? "Analyzing…" : "📜 Syntax Analysis"}
        </button>
      </div>
      {syntax && <pre className="markdown" style={{ marginTop: 8 }}>{syntax}</pre>}
      {msg && <p className="success" style={{ marginTop: 8, fontSize: 13 }}>{msg}</p>}
      {error && <p className="error" style={{ marginTop: 8 }}>{error}</p>}
    </div>
  );
}

function TermDetail({
  domain,
  term,
  onNavigate,
}: {
  domain: Domain;
  term: Term;
  onNavigate: (t: Term) => void;
}) {
  const [current, setCurrent] = useState(term);
  const [definition, setDefinition] = useState(term.definition ?? "");
  const [frequency, setFrequency] = useState(term.frequency);
  const [context, setContext] = useState("");
  const [sentence, setSentence] = useState("");
  const [explainResult, setExplainResult] = useState<{ translation: string; explanation: string } | null>(null);
  const [analysis, setAnalysis] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const [siblings, setSiblings] = useState<Term[]>([]);
  const [linked, setLinked] = useState<Sentence[]>([]);
  const [linkQuery, setLinkQuery] = useState("");
  const [linkResults, setLinkResults] = useState<Sentence[]>([]);
  const [images, setImages] = useState<string[]>(current.image_paths ?? []);

  const defJob = useJob<{ definition: string }>();
  const explainJob = useJob<{ translation: string; explanation: string }>();
  const syntaxJob = useJob<{ analysis: string }>();
  const ttsJob = useJob<{ url: string }>();
  const imageJob = useJob<{ image_paths: string[] }>();

  useEffect(() => {
    api.listTerms(domain.id).then(setSiblings).catch(() => {});
    api.listSentencesForTerm(current.id).then(setLinked).catch(() => {});
  }, [domain.id, current.id]);

  const index = siblings.findIndex((t) => t.id === current.id);
  const prev = index > 0 ? siblings[index - 1] : null;
  const next = index >= 0 && index < siblings.length - 1 ? siblings[index + 1] : null;

  const save = async (patch: Partial<Term>) => {
    setError("");
    try {
      await api.updateTerm(current.id, patch);
      setCurrent({ ...current, ...patch });
    } catch (e) {
      setError(String(e));
    }
  };

  const generateDefinition = async () => {
    setBusy("definition");
    setError("");
    try {
      const { definition: d } = await defJob.run(() => api.enqueueGenerateDefinition(current.word));
      setDefinition(d);
      await api.updateTerm(current.id, { definition: d });
      setCurrent({ ...current, definition: d });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  };

  const explain = async () => {
    if (!context.trim()) return;
    setBusy("explain");
    setError("");
    try {
      setExplainResult(await explainJob.run(() => api.enqueueExplain(current.word, context.trim())));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  };

  const analyze = async () => {
    if (!sentence.trim()) return;
    setBusy("analyze");
    setError("");
    try {
      const { analysis: a } = await syntaxJob.run(() => api.enqueueAnalyzeSyntax(sentence.trim()));
      setAnalysis(a);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  };

  const speak = async (text: string) => {
    setBusy("tts");
    setError("");
    try {
      const { url } = await ttsJob.run(() => api.enqueueTts(text));
      const audio = new Audio(url);
      await audio.play();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  };

  const genPronunciation = async () => {
    setBusy("tts");
    setError("");
    try {
      const { url } = await ttsJob.run(() => api.enqueueTts(current.word));
      setCurrent({ ...current, audio_hash: url });
      await api.updateTerm(current.id, { audio_hash: url });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  };

  const link = async (s: Sentence) => {
    setError("");
    try {
      await api.linkTermToSentence(current.id, s.id);
      setLinked(await api.listSentencesForTerm(current.id));
    } catch (e) {
      setError(String(e));
    }
  };

  const searchLink = async (semantic: boolean) => {
    if (!linkQuery.trim()) return;
    setError("");
    try {
      setLinkResults(
        semantic
          ? await api.semanticSearch(domain.id, linkQuery.trim())
          : await api.searchSentences(domain.id, linkQuery.trim())
      );
    } catch (e) {
      setError(String(e));
    }
  };

  const fetchImages = async (regenerate: boolean) => {
    setBusy("images");
    setError("");
    try {
      const { image_paths } = await imageJob.run(() =>
        api.enqueueImageFetch(current.word, current.definition ?? "", context.trim(), regenerate)
      );
      setImages(image_paths);
      await api.updateTerm(current.id, { image_paths });
      setCurrent({ ...current, image_paths });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  };

  return (
    <div>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row">
            <h2 className="term-word">{current.word}</h2>
            <button onClick={() => speak(current.word)} disabled={busy === "tts"} title="Play TTS">
              {busy === "tts" ? "…" : "🔊"}
            </button>
          </div>
          <Stars value={current.star_level} onChange={(v) => save({ star_level: v })} />
        </div>
        <div className="row" style={{ marginTop: 4, justifyContent: "space-between" }}>
          <div className="row">
            <span className="muted">{domain.name}</span>
            <span className="muted" style={{ marginLeft: 8 }}>
              frequency
            </span>
            <input
              type="number"
              value={frequency}
              min={0}
              style={{ width: 70 }}
              onChange={(e) => setFrequency(Number(e.target.value))}
              onBlur={() => frequency !== current.frequency && save({ frequency })}
            />
          </div>
          <div className="row">
            <button onClick={() => prev && onNavigate(prev)} disabled={!prev}>
              ← Prev
            </button>
            <span className="muted" style={{ fontSize: 13 }}>
              {index >= 0 ? `${index + 1} / ${siblings.length}` : ""}
            </span>
            <button onClick={() => next && onNavigate(next)} disabled={!next}>
              Next →
            </button>
          </div>
        </div>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Pronunciation</h3>
        <div className="row" style={{ marginBottom: 8 }}>
          {current.audio_hash ? (
            <audio controls src={current.audio_hash} style={{ flex: 1, height: 32 }} />
          ) : (
            <span className="muted" style={{ fontSize: 13 }}>🔇 No audio available</span>
          )}
          <button onClick={genPronunciation} disabled={busy === "tts"}>
            {busy === "tts" ? "Generating…" : "✨ Gen Pronunciation"}
          </button>
        </div>
        <MicRecorder />
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Definition</h3>
        <textarea
          value={definition}
          onChange={(e) => setDefinition(e.target.value)}
          placeholder="Definition (English + Chinese)"
        />
        <div className="row" style={{ marginTop: 8 }}>
          <button className="primary" onClick={() => save({ definition })} disabled={!definition.trim()}>
            Save
          </button>
          <button onClick={generateDefinition} disabled={busy === "definition"}>
            {busy === "definition" ? "Generating…" : "AI generate"}
          </button>
        </div>
        <p style={{ marginTop: 8 }}>
          <a
            href={`https://www.google.com/search?tbm=isch&q=${encodeURIComponent(current.word)}`}
            target="_blank"
            rel="noreferrer"
          >
            ↳ 🔍 Search Images on Google
          </a>
        </p>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Explain in context</h3>
        <div className="row grow">
          <input
            placeholder="Paste a sentence containing this term"
            value={context}
            onChange={(e) => setContext(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && explain()}
          />
          <button className="primary" onClick={explain} disabled={!context.trim() || busy === "explain"}>
            {busy === "explain" ? "Explaining…" : "Explain"}
          </button>
        </div>
        {explainResult && (
          <div style={{ marginTop: 10 }}>
            <p>
              <span className="badge">translation</span> {explainResult.translation}
            </p>
            <p>
              <span className="badge">explanation</span> {explainResult.explanation}
            </p>
          </div>
        )}
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Images</h3>
        <div className="row" style={{ marginBottom: 8 }}>
          <button onClick={() => fetchImages(false)} disabled={busy === "images"}>
            {busy === "images" ? "Fetching…" : "Fetch images"}
          </button>
          <button onClick={() => fetchImages(true)} disabled={busy === "images"}>
            Regenerate
          </button>
        </div>
        {images.length > 0 ? (
          <div className="row">
            {images.map((src) => (
              <img key={src} src={src} alt={current.word} className="term-img" />
            ))}
          </div>
        ) : (
          <p className="muted">No images yet. Fetch some to visualize this term.</p>
        )}
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Linked sentences</h3>
        {linked.length === 0 ? (
          <p className="muted">No sentences linked yet. Search below and link one.</p>
        ) : (
          linked.map((s) => <SentenceCard key={s.id} term={current} sentence={s} />)
        )}
        <div className="row grow" style={{ marginTop: 8 }}>
          <input
            placeholder="Search sentences to link…"
            value={linkQuery}
            onChange={(e) => setLinkQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && searchLink(false)}
          />
          <button onClick={() => searchLink(false)}>Keyword</button>
          <button onClick={() => searchLink(true)}>Semantic</button>
        </div>
        {linkResults.length > 0 && (
          <div className="list" style={{ marginTop: 8 }}>
            {linkResults.map((s) => (
              <div key={s.id} className="list-item">
                <span>{s.content_en}</span>
                <button onClick={() => link(s)}>Link</button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Syntax analysis</h3>
        <div className="row grow">
          <input
            placeholder="Paste a sentence to analyze"
            value={sentence}
            onChange={(e) => setSentence(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && analyze()}
          />
          <button className="primary" onClick={analyze} disabled={!sentence.trim() || busy === "analyze"}>
            {busy === "analyze" ? "Analyzing…" : "Analyze"}
          </button>
        </div>
        {analysis && (
          <div style={{ marginTop: 10 }}>
            <pre className="markdown">{analysis}</pre>
          </div>
        )}
      </div>

      {error && (
        <div className="panel">
          <p className="error">{error}</p>
        </div>
      )}
    </div>
  );
}
