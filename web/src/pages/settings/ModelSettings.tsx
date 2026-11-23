import type { Model, Profile } from "../../types";

type Props = {
  profile: Profile;
  cpuThreads: number;
  models: Model[];
  onProfileChange: (fields: Partial<Profile>) => void;
  onThreadsChange: (threads: number) => void;
  onInstall: (id: string) => void;
};

export function ModelSettings({
  profile,
  cpuThreads,
  models,
  onProfileChange,
  onThreadsChange,
  onInstall,
}: Props) {
  return (
    <section className="card settings-section">
      <div className="section-title">
        <div>
          <h2>本地转录模型</h2>
          <p>语音在当前设备识别，无需云端转录服务。</p>
        </div>
      </div>
      <div className="form-grid">
        <label>
          默认模型
          <select
            value={profile.model_id}
            onChange={(e) => onProfileChange({ model_id: e.target.value })}
          >
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id} · {m.state === "ready" ? "已安装" : "未安装"}
              </option>
            ))}
          </select>
        </label>
        <label>
          每项任务 CPU 线程上限
          <input
            type="number"
            min={1}
            max={32}
            value={cpuThreads}
            onChange={(e) => onThreadsChange(Number(e.target.value))}
          />
          <small>实际线程数由系统按 CPU 和内存负载分配。</small>
        </label>
      </div>
      <div className="model-grid">
        {models.map((m) => (
          <div
            className={`model-item ${profile.model_id === m.id ? "chosen" : ""}`}
            key={m.id}
          >
            <strong>{m.id}</strong>
            <span className={m.state === "ready" ? "ready-text" : "muted"}>
              {m.state === "ready" ? "● 已安装" : "等待安装"}
            </span>
            {m.state === "ready" ? (
              <button
                className="text-button"
                onClick={() => onProfileChange({ model_id: m.id })}
              >
                {profile.model_id === m.id ? "当前选择" : "选择"}
              </button>
            ) : (
              <button className="text-button" onClick={() => onInstall(m.id)}>
                安装模型 ↗
              </button>
            )}
          </div>
        ))}
      </div>
      <label>
        术语提示
        <textarea
          value={profile.glossary}
          onChange={(e) => onProfileChange({ glossary: e.target.value })}
          placeholder="例如：PostgreSQL、EBITDA、Reelucid"
          rows={2}
          maxLength={2000}
        />
        <small>用于识别提示，不自动替换原文中的数字和否定表达。</small>
      </label>
    </section>
  );
}
