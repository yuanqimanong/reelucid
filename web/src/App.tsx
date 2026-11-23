import { useState, useEffect } from "react";
import type { View, Settings, Model, Job, XhsStatus } from "./types";
import { Brand, Icon, Notice } from "./components/ui";
import { terminal } from "./format";
import { NewTask } from "./pages/NewTask";
import { TaskList } from "./pages/TaskList";
import { TaskDetail } from "./pages/TaskDetail";
import { SettingsPage } from "./pages/SettingsPage";
import { ComponentsPage } from "./pages/ComponentsPage";
import { api, json } from "./api";

type Capabilities = { version: string; asr: Model[]; xhs_resolver: XhsStatus };

const navigation = [
  { id: "new", icon: "plus", label: "新建任务" },
  { id: "tasks", icon: "tasks", label: "任务与文稿" },
  { id: "settings", icon: "settings", label: "模型与设置" },
  { id: "components", icon: "components", label: "组件" },
] as const;

export function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null),
    [token, setToken] = useState(""),
    [view, setView] = useState<View>("new"),
    [selected, setSelected] = useState<string | null>(null);
  const [config, setConfig] = useState<Settings | null>(null),
    [capabilities, setCapabilities] = useState<Capabilities | null>(null),
    [credentials, setCredentials] = useState<string[]>([]),
    [jobs, setJobs] = useState<Job[]>([]),
    [nextCursor, setNextCursor] = useState<number | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const refreshConfig = async () => {
    const [settings, serverCapabilities] = await Promise.all([
      api<{ config: Settings; credential_ids: string[] }>(
        "/admin/system-config",
      ),
      api<Capabilities>("/capabilities"),
    ]);
    setConfig(settings.config);
    setCredentials(settings.credential_ids);
    setCapabilities(serverCapabilities);
    setAuthenticated(true);
  };
  const refreshJobs = async () => {
    try {
      const page = await api<{ items: Job[]; next_cursor: number | null }>(
        "/jobs?limit=100",
      );
      setJobs(page.items);
      setNextCursor(page.next_cursor);
    } catch (e) {
      if (authenticated) setError((e as Error).message);
    }
  };
  useEffect(() => {
    refreshConfig().catch(() => setAuthenticated(false));
    const expired = () => setAuthenticated(false);
    window.addEventListener("session-expired", expired);
    return () => window.removeEventListener("session-expired", expired);
  }, []);
  useEffect(() => {
    if (!authenticated) return;
    void refreshJobs();
    const timer = setInterval(() => void refreshJobs(), 3000);
    return () => clearInterval(timer);
  }, [authenticated]);
  const open = (id: string) => {
    setSelected(id);
    setView("tasks");
    void refreshJobs();
  };
  const navigate = (next: View) => {
    if (next === "settings" || next === "new")
      void refreshConfig().catch((e) => setError((e as Error).message));
    setView(next);
    setSelected(null);
    setError("");
  };
  if (authenticated === null)
    return (
      <div className="splash">
        <Brand />
        <p>正在连接工作台…</p>
      </div>
    );
  if (!authenticated)
    return (
      <div className="login">
        <section className="login-story">
          <Brand />
          <div className="login-title">
            <h1>
              音视频转录
              <br />
              工作台
            </h1>
            <p>在本地整理文稿，按时间回到原片。</p>
          </div>
          <figure className="login-art">
            <div
              className="mountain-scroll"
              role="img"
              aria-label="米友仁笔下的远山与云雾"
            />
            <figcaption>
              米友仁 · 云山图局部 <span>1130</span>
            </figcaption>
          </figure>
        </section>
        <section className="login-form">
          <div>
            <h2>登录工作台</h2>
            <p className="muted">使用部署时生成的管理员 Token 登录。</p>
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                setBusy(true);
                setError("");
                try {
                  await api("/auth/session", json({ token }));
                  setToken("");
                  await refreshConfig();
                } catch (err) {
                  setError((err as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              <label>
                管理员 Token
                <input
                  type="password"
                  autoComplete="current-password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder="粘贴管理员 Token"
                  required
                />
              </label>
              {error && <Notice error>{error}</Notice>}
              <button className="primary wide" disabled={busy}>
                {busy ? "正在连接…" : "进入工作台"}
                <Icon name="arrow" />
              </button>
            </form>
            <div className="help">
              <Icon name="lock" />
              <span>
                在部署终端运行 <code>reelucid init</code> 可查看 Token
                文件位置。
              </span>
            </div>
          </div>
        </section>
      </div>
    );
  const active = jobs.filter((j) => !terminal(j.status)).length;
  const models = capabilities?.asr ?? [];
  const xhs = capabilities?.xhs_resolver;
  return (
    <div className="app">
      <aside className="sidebar">
        <Brand />
        <nav aria-label="工作台导航">
          {navigation.map(({ id, icon, label }) => (
            <button
              key={id}
              className={view === id ? "active" : ""}
              onClick={() => navigate(id)}
              title={label}
              aria-current={view === id ? "page" : undefined}
            >
              <Icon name={icon} />
              <span>{label}</span>
              {id === "tasks" && active > 0 && <b>{active}</b>}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-state">
            <i />
            <span>
              {models.some((m) => m.state === "ready")
                ? "本地模型已安装"
                : "需要安装本地模型"}
              <small>
                {models.filter((m) => m.state === "ready").length} 个已安装模型
              </small>
            </span>
          </div>
          <button
            className="logout"
            title="退出工作台"
            onClick={async () => {
              await api("/auth/session", { method: "DELETE" });
              setAuthenticated(false);
            }}
          >
            <Icon name="logout" />
            <span>退出工作台</span>
          </button>
          {capabilities && (
            <small className="version">
              REELUCID / v{capabilities.version}
            </small>
          )}
        </div>
      </aside>
      <main>
        <header className="topbar">
          <span>
            工作台 <b>/</b>{" "}
            {view === "tasks" && selected
              ? "任务详情"
              : navigation.find((item) => item.id === view)?.label}
          </span>
          <div className="top-status">
            <i />
            {active ? `${active} 项任务进行中` : "当前无进行中任务"}
          </div>
        </header>
        <div className="page">
          {error && <Notice error>{error}</Notice>}
          {view === "new" && config && xhs && (
            <NewTask
              config={config}
              xhsConfigured={xhs.configured}
              models={models}
              credentials={credentials}
              onOpen={open}
              onSettings={() => navigate("settings")}
            />
          )}
          {view === "tasks" &&
            (selected ? (
              <TaskDetail
                key={selected}
                id={selected}
                config={config!}
                onBack={() => setSelected(null)}
                onOpen={open}
              />
            ) : (
              <TaskList
                jobs={jobs}
                nextCursor={nextCursor}
                onOpen={open}
                onNew={() => navigate("new")}
              />
            ))}
          {view === "settings" && config && xhs && (
            <SettingsPage
              config={config}
              xhsLocalAvailable={xhs.local_available}
              models={models}
              credentials={credentials}
              onRefresh={refreshConfig}
              onOpen={open}
            />
          )}
          {view === "components" && (
            <ComponentsPage onSettings={() => navigate("settings")} />
          )}
        </div>
      </main>
    </div>
  );
}
