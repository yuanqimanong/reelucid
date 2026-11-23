import { useEffect, useState } from "react";
import { api } from "../api";
import { Icon, Notice } from "../components/ui";

type ComponentId = "dtk" | "xhs";
type ServiceState =
  | "unknown"
  | "not_installed"
  | "stopped"
  | "starting"
  | "healthy"
  | "unhealthy";
type InstallState = "idle" | "queued" | "running" | "succeeded" | "failed";
type Service = {
  id: ComponentId;
  state: ServiceState;
  install_state: InstallState;
  error_code?: string | null;
};
type Overview = { agent_connected: boolean; components: Service[] };

const packages = {
  dtk: {
    name: "DTK",
    description:
      "为抖音、TikTok 提供专用视频解析。安装包含 PostgreSQL、Redis 和 DTK 服务。",
    endpoint: "http://reelucid-managed-dtk:8000",
  },
  xhs: {
    name: "XHS-Downloader",
    description:
      "为小红书作品提供独立解析服务。Reelucid 通过 Docker 网络连接此组件。",
    endpoint: "http://xhs-resolver:5556",
  },
} as const;
const serviceLabels: Record<ServiceState, string> = {
  unknown: "状态未知",
  not_installed: "未安装",
  stopped: "已停止",
  starting: "启动中",
  healthy: "运行正常",
  unhealthy: "运行异常",
};
const installLabels: Record<InstallState, string> = {
  idle: "",
  queued: "等待安装",
  running: "正在安装",
  succeeded: "安装完成",
  failed: "安装失败",
};

function Command({ value }: { value: string }) {
  const [feedback, setFeedback] = useState("");
  return (
    <div className="command">
      <code>{value}</code>
      <button
        className="copy"
        aria-label={`复制命令 ${value}`}
        onClick={async () => {
          try {
            if (!navigator.clipboard) throw new Error();
            await navigator.clipboard.writeText(value);
            setFeedback("已复制");
          } catch {
            setFeedback("请选中命令，手动复制。");
          }
        }}
      >
        <Icon name="copy" size={15} />
        复制
      </button>
      {feedback && <small role="status">{feedback}</small>}
    </div>
  );
}

export function ComponentsPage({ onSettings }: { onSettings: () => void }) {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState<ComponentId | null>(null);
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const overview = await api<Overview>("/admin/components", {
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setData(overview);
          setError("");
        }
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 4000);
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [revision]);

  async function install(id: ComponentId) {
    setSubmitting(id);
    setActionError("");
    setMessage("");
    try {
      await api(`/admin/components/${id}/install`, { method: "POST" });
      setData(
        (current) =>
          current && {
            ...current,
            components: current.components.map((item) =>
              item.id === id ? { ...item, install_state: "queued" } : item,
            ),
          },
      );
      setMessage(`${packages[id].name} 安装请求已提交。`);
      setRevision((n) => n + 1);
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setSubmitting(null);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>组件</h1>
          <p>管理部署主机上的可选视频解析服务。</p>
        </div>
      </div>
      <section className="card core-component">
        <span className="file-tile">
          <Icon name="components" />
        </span>
        <div>
          <h2>核心服务</h2>
          <p>Web、API、本地转录与 SQLite 随 Reelucid 单容器启动。</p>
        </div>
        <span className="badge succeeded">
          <i />
          已内置
        </span>
      </section>
      {error && <Notice error>组件状态读取失败。{error}</Notice>}
      {actionError && <Notice error>{actionError}</Notice>}
      {message && <Notice>{message}</Notice>}
      <div className="components-grid">
        {(Object.keys(packages) as ComponentId[]).map((id) => {
          const pkg = packages[id];
          const item = data?.components.find((row) => row.id === id);
          const state = item?.state ?? "unknown";
          const task = item?.install_state ?? "idle";
          const busy = task === "queued" || task === "running";
          const disabled =
            !data?.agent_connected ||
            !!error ||
            !item ||
            !!submitting ||
            busy ||
            state === "healthy" ||
            state === "starting";
          return (
            <section className="card component-card" key={id}>
              <div className="component-title">
                <h2>{pkg.name}</h2>
                <span
                  className={`badge ${state === "healthy" ? "succeeded" : state === "unhealthy" ? "failed" : ""}`}
                >
                  <i />
                  {data ? serviceLabels[state] : error ? "状态未知" : "读取中"}
                </span>
              </div>
              <p>{pkg.description}</p>
              <div className="service-endpoint">
                <span>解析服务地址</span>
                <code>{pkg.endpoint}</code>
              </div>
              <div className="component-action">
                <button
                  className="primary"
                  disabled={disabled}
                  onClick={() => void install(id)}
                >
                  <Icon name="download" size={16} />
                  {submitting === id
                    ? "提交中…"
                    : busy
                      ? installLabels[task]
                      : state === "healthy"
                        ? "已运行"
                        : state === "starting"
                          ? "正在启动"
                          : state === "stopped"
                            ? "启动组件"
                            : task === "failed" || state === "unhealthy"
                              ? "重试安装"
                              : "安装组件"}
                </button>
                {(task === "failed" || task === "succeeded") && (
                  <small
                    role="status"
                    className={
                      task === "failed" ? "failure-text" : "ready-text"
                    }
                  >
                    {installLabels[task]} {item?.error_code}
                  </small>
                )}
              </div>
              <details>
                <summary>在宿主机直接安装</summary>
                <Command
                  value={`uv run --locked --extra server reelucid components install ${id}`}
                />
              </details>
            </section>
          );
        })}
      </div>
      <section className="card agent-panel">
        <div className="component-title">
          <h2>宿主机安装代理</h2>
          <span
            className={`badge ${data?.agent_connected && !error ? "succeeded" : ""}`}
          >
            <i />
            {error
              ? "连接状态未知"
              : data
                ? data.agent_connected
                  ? "已连接"
                  : "未连接"
                : "读取中"}
          </span>
        </div>
        <p>
          需要从网页安装时，在部署主机的项目目录运行下列命令，并保持终端开启。
        </p>
        <Command value="uv run --locked --extra server reelucid components agent" />
        <p>
          安装后在「模型与设置」中启用对应解析服务，填写服务地址和所需凭据。DTK
          的本机管理地址为 <code>http://127.0.0.1:18080</code>。
        </p>
        <button className="text-button" onClick={onSettings}>
          打开模型与设置 <Icon name="arrow" size={15} />
        </button>
      </section>
    </>
  );
}
