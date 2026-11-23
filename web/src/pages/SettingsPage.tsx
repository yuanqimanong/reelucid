import { useState, useEffect } from "react";
import type { Settings, Model, Profile, Job } from "../types";
import { Icon, Notice } from "../components/ui";
import { api } from "../api";
import { AiSettings } from "./settings/AiSettings";
import { ModelSettings } from "./settings/ModelSettings";
import { SourceSettings } from "./settings/SourceSettings";
import { saveSettings, type PendingCredentials } from "./settings/saveSettings";

export function SettingsPage({
  config,
  xhsLocalAvailable,
  models,
  credentials,
  onRefresh,
  onOpen,
}: {
  config: Settings;
  xhsLocalAvailable: boolean;
  models: Model[];
  credentials: string[];
  onRefresh: () => Promise<void>;
  onOpen: (id: string) => void;
}) {
  const [draft, setDraft] = useState<Settings>(() => structuredClone(config));
  const [pending, setPending] = useState<PendingCredentials>({
    providerKey: "",
    dtkKey: "",
    cookieId: "bilibili",
    cookie: "",
  });
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [raw, setRaw] = useState("");
  useEffect(() => {
    setDraft(structuredClone(config));
  }, [config]);
  function updatePending(fields: Partial<PendingCredentials>) {
    setPending((current) => ({ ...current, ...fields }));
  }
  function updateDraft(fields: Partial<Settings>) {
    setDraft((current) => ({ ...current, ...fields }));
  }
  function updateProfile(fields: Partial<Profile>) {
    setDraft((current) => ({
      ...current,
      processing_profiles: [
        { ...current.processing_profiles[0], ...fields },
        ...current.processing_profiles.slice(1),
      ],
    }));
  }
  async function installModel(id: string) {
    try {
      const job = await api<Job>(`/admin/asr-models/${id}/install`, {
        method: "POST",
      });
      onOpen(job.job_id);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function testProvider(id: string) {
    try {
      const result = await api<{ elapsed_ms: number; reply: string }>(
        `/admin/providers/${encodeURIComponent(id)}/test`,
        { method: "POST" },
      );
      setMessage(`连接成功 · ${result.elapsed_ms} ms · ${result.reply}`);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function deleteCredential(id: string) {
    if (!confirm(`移除凭据 ${id}？依赖它的任务将无法继续调用。`)) return;
    try {
      await api(`/admin/credentials/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      await onRefresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function save() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const settings = advanced ? (JSON.parse(raw) as Settings) : draft;
      const saved = await saveSettings(settings, pending, (field, next) => {
        updatePending({ [field]: "" });
        if (advanced) {
          // The form can render only a validated config. Keep unvalidated edits in JSON.
          setRaw(JSON.stringify(next, null, 2));
          setDraft((current) => ({
            ...current,
            revision: current.revision + 1,
          }));
        } else {
          setDraft(next);
        }
      });
      setDraft(saved);
      if (advanced) setRaw(JSON.stringify(saved, null, 2));
      await onRefresh();
      setMessage("设置已保存。新任务将使用这份配置。");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>模型与设置</h1>
          <p>为新任务配置转录模型、AI 服务和视频来源。</p>
        </div>
        <button className="primary" disabled={busy} onClick={() => void save()}>
          {busy ? "保存中…" : "保存设置"}
          <Icon name="check" size={17} />
        </button>
      </div>
      {message && <Notice>{message}</Notice>}
      {error && <Notice error>{error}</Notice>}
      <div className="settings-layout" inert={busy}>
        <ModelSettings
          profile={draft.processing_profiles[0]}
          cpuThreads={draft.cpu_threads}
          models={models}
          onThreadsChange={(cpu_threads) => updateDraft({ cpu_threads })}
          onProfileChange={updateProfile}
          onInstall={installModel}
        />
        <AiSettings
          draft={draft}
          secret={pending.providerKey}
          onChange={updateDraft}
          onProfileChange={updateProfile}
          onKeyChange={(providerKey) => updatePending({ providerKey })}
          onTest={testProvider}
        />
        <SourceSettings
          draft={draft}
          savedXhs={config.xhs_resolver}
          xhsLocalAvailable={xhsLocalAvailable}
          credentials={credentials}
          pending={pending}
          onChange={updateDraft}
          onPendingChange={updatePending}
          onDelete={deleteCredential}
        />
        <section className="card settings-section">
          <div className="section-title">
            <div>
              <h2>资源限制</h2>
              <p>保留足够的磁盘空间，让长内容稳定处理。</p>
            </div>
          </div>
          <div className="form-grid">
            <label>
              上传上限 / MiB
              <input
                type="number"
                min={1}
                max={8192}
                value={draft.max_upload_mb}
                onChange={(e) =>
                  updateDraft({ max_upload_mb: Number(e.target.value) })
                }
              />
            </label>
            <label>
              最长媒体 / 小时
              <input
                type="number"
                min={1}
                max={24}
                value={draft.max_duration_seconds / 3600}
                onChange={(e) =>
                  updateDraft({
                    max_duration_seconds: Number(e.target.value) * 3600,
                  })
                }
              />
            </label>
            <label>
              队列上限
              <input
                type="number"
                min={1}
                max={1000}
                value={draft.queue_limit}
                onChange={(e) =>
                  updateDraft({ queue_limit: Number(e.target.value) })
                }
              />
            </label>
          </div>
          <p className="muted small">
            部署硬上限始终优先。修改不影响已经提交的任务。
          </p>
          <button
            className="text-button"
            onClick={() => {
              setAdvanced(!advanced);
              setRaw(JSON.stringify(draft, null, 2));
            }}
          >
            高级：完整预设与模型角色配置 {advanced ? "−" : "+"}
          </button>
          {advanced && (
            <label className="advanced-editor">
              配置 JSON
              <textarea
                spellCheck={false}
                rows={20}
                value={raw}
                onChange={(e) => setRaw(e.target.value)}
              />
              <small>保存时进行字段和引用校验；不包含任何明文凭据。</small>
            </label>
          )}
        </section>
      </div>
    </>
  );
}
