import { useState, useRef } from "react";
import type { Settings, Model, Job } from "../types";
import { Icon, Notice } from "../components/ui";
import { bytes } from "../format";
import { api, json } from "../api";
export function NewTask({
  config,
  xhsConfigured,
  models,
  transcodePresets,
  credentials,
  onOpen,
  onSettings,
}: {
  config: Settings;
  xhsConfigured: boolean;
  models: Model[];
  transcodePresets: string[];
  credentials: string[];
  onOpen: (id: string) => void;
  onSettings: () => void;
}) {
  const [mode, setMode] = useState<"file" | "url">("file"),
    [file, setFile] = useState<File | null>(null),
    [url, setUrl] = useState(""),
    [adapter, setAdapter] = useState(""),
    [profile, setProfile] = useState(
      config.processing_profiles[0]?.id ?? "local-default",
    ),
    [language, setLanguage] = useState("auto"),
    [transcribe, setTranscribe] = useState(true),
    [transcodePreset, setTranscodePreset] = useState(""),
    [summary, setSummary] = useState(false),
    [cookie, setCookie] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [dragging, setDragging] = useState(false);
  const doTranscribe = transcribe || transcodePresets.length === 0;
  const preset = transcodePresets.includes(transcodePreset)
    ? transcodePreset
    : "";
  const input = useRef<HTMLInputElement>(null),
    selectedProfile = config.processing_profiles.find((p) => p.id === profile),
    ready = models.some(
      (m) => m.id === selectedProfile?.model_id && m.state === "ready",
    );
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      let source: object;
      if (mode === "file") {
        if (!file) throw new Error("请先选择音视频文件。");
        const form = new FormData();
        form.append("file", file);
        const asset = await api<{ asset_id: string }>("/assets", {
          method: "POST",
          body: form,
        });
        source = { kind: "asset", asset_id: asset.asset_id };
      } else
        source = {
          kind: "url",
          url,
          ...(adapter ? { adapter_id: adapter } : {}),
          ...(cookie ? { credential_profile_id: cookie } : {}),
        };
      const job = await api<Job>(
        "/jobs",
        json({
          source,
          processing_profile_id: profile,
          operations: [
            ...(doTranscribe
              ? [{ id: "transcript", type: "transcribe", required: true }]
              : []),
            ...(preset
              ? [{ id: "media", type: "transcode", options: { preset } }]
              : []),
            ...(doTranscribe && summary
              ? [
                  {
                    id: "summary",
                    type: "summarize",
                    required: false,
                    options: { output_language: "zh" },
                  },
                ]
              : []),
          ],
          options: {
            language,
            keep_source: true,
            export_formats: ["json", "srt", "vtt", "txt", "md"],
          },
        }),
      );
      onOpen(job.job_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="page-heading import-heading">
        <div>
          <h1>新建任务</h1>
          <p>导入音视频，生成文稿、字幕或转码文件。</p>
        </div>
      </div>
      <div className="new-layout">
        <form className="card import-card" onSubmit={submit}>
          <div className="section-title">
            <h2>添加内容</h2>
          </div>
          <div className="segmented">
            <button
              type="button"
              className={mode === "file" ? "selected" : ""}
              aria-pressed={mode === "file"}
              onClick={() => setMode("file")}
            >
              <Icon name="upload" size={17} />
              本地文件
            </button>
            <button
              type="button"
              className={mode === "url" ? "selected" : ""}
              aria-pressed={mode === "url"}
              onClick={() => setMode("url")}
            >
              <Icon name="link" size={17} />
              视频链接
            </button>
          </div>
          {mode === "file" ? (
            <div
              className={`dropzone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""}`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                setFile(e.dataTransfer.files[0] ?? null);
              }}
            >
              <div className="upload-symbol">
                <Icon name={file ? "check" : "upload"} size={28} />
              </div>
              <h3>{file ? file.name : "把文件拖到这里"}</h3>
              <p>
                {file ? bytes(file.size) : "或从电脑中选择一段视频、访谈或播客"}
              </p>
              <button
                type="button"
                className="secondary"
                onClick={() => input.current?.click()}
              >
                {file ? "重新选择" : "选择音视频文件"}
              </button>
              <input
                ref={input}
                type="file"
                accept="video/*,audio/*,.mkv,.flac"
                hidden
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
              <small>
                MP4、MKV、WebM、MP3、WAV 等 · 上限 {config.max_upload_mb} MiB
              </small>
            </div>
          ) : (
            <div className="url-input">
              <label>
                视频或音频地址
                <input
                  type="text"
                  inputMode="url"
                  required
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="视频 URL，或含一条链接的分享文案"
                />
              </label>
              <p>
                支持 yt-dlp
                可解析的单条视频及媒体直链。站点、地区和登录要求可能影响下载。
              </p>
              <label>
                下载方式
                <select
                  value={adapter}
                  onChange={(e) => setAdapter(e.target.value)}
                >
                  <option value="">自动选择</option>
                  {Object.entries(config.adapters)
                    .filter(
                      ([id, enabled]) =>
                        enabled &&
                        id !== "upload" &&
                        (id !== "dtk" || config.source_resolver.enabled) &&
                        (id !== "xhs" || xhsConfigured),
                    )
                    .map(([id]) => (
                      <option key={id} value={id}>
                        {(
                          {
                            http_media: "HTTP 媒体直链",
                            yt_dlp: "yt-dlp 通用站点",
                            bilibili: "Bilibili 公开 API",
                            dtk: "DTK v5 解析服务",
                            xhs: "小红书 XHS-Downloader",
                          } as Record<string, string>
                        )[id] ?? id}
                      </option>
                    ))}
                </select>
              </label>
              {credentials.length > 0 && (
                <label>
                  站点凭据
                  <select
                    value={cookie}
                    onChange={(e) => setCookie(e.target.value)}
                  >
                    <option value="">不使用凭据</option>
                    {credentials.map((c) => (
                      <option key={c}>{c}</option>
                    ))}
                  </select>
                </label>
              )}
            </div>
          )}
          <div className="section-title second">
            <h2>处理方式</h2>
          </div>
          {transcodePresets.length > 0 && (
            <>
              <label className="toggle-line">
                <input
                  type="checkbox"
                  checked={doTranscribe}
                  onChange={(e) => setTranscribe(e.target.checked)}
                />
                <strong>语音转录</strong>
              </label>
              <label>
                媒体转码
                <select
                  value={preset}
                  onChange={(e) => setTranscodePreset(e.target.value)}
                >
                  <option value="">关闭</option>
                  {transcodePresets.map((id) => (
                    <option key={id} value={id}>
                      {(
                        {
                          audio_mp3: "MP3 音频",
                          audio_wav: "WAV 音频",
                          video_mp4: "MP4 视频",
                        } as Record<string, string>
                      )[id] ?? id}
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}
          <div className="form-grid">
            <label>
              处理预设
              <select
                value={profile}
                disabled={!doTranscribe}
                onChange={(e) => setProfile(e.target.value)}
              >
                {config.processing_profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.id} · {p.model_id}
                  </option>
                ))}
              </select>
            </label>
            <label>
              语音语言
              <select
                value={language}
                disabled={!doTranscribe}
                onChange={(e) => setLanguage(e.target.value)}
              >
                <option value="auto">自动识别</option>
                <option value="zh">中文</option>
                <option value="en">英语</option>
                <option value="ja">日语</option>
                <option value="ko">韩语</option>
                <option value="fr">法语</option>
                <option value="de">德语</option>
                <option value="es">西班牙语</option>
              </select>
            </label>
          </div>
          <label className="toggle-line">
            <input
              type="checkbox"
              checked={doTranscribe && summary}
              disabled={!doTranscribe}
              onChange={(e) => setSummary(e.target.checked)}
            />
            <span>
              <strong>同时生成 AI 摘要</strong>
              <small>使用所选预设的文本模型。摘要失败时仍可下载文稿。</small>
            </span>
            <Icon name="spark" />
          </label>
          {doTranscribe && !ready && (
            <Notice>
              当前预设的 {selectedProfile?.model_id} 模型尚未安装。
              <button
                type="button"
                className="text-button"
                onClick={onSettings}
              >
                前往模型设置
              </button>
            </Notice>
          )}
          {error && <Notice error>{error}</Notice>}
          <div className="submit-row">
            <button
              className="primary"
              disabled={
                busy ||
                (doTranscribe ? !ready : !preset) ||
                (mode === "file" && !file)
              }
            >
              {busy ? "正在提交…" : "开始处理"}
              <Icon name="arrow" size={18} />
            </button>
          </div>
        </form>
        {doTranscribe && (
          <aside className="new-aside">
            <section className="task-spec">
              <h2>本次转录</h2>
              <dl>
                <div>
                  <dt>转录模型</dt>
                  <dd>{selectedProfile?.model_id ?? "未选择"}</dd>
                </div>
                <div>
                  <dt>推理设备</dt>
                  <dd>{selectedProfile?.device.toUpperCase() ?? "—"}</dd>
                </div>
                <div>
                  <dt>最长时长</dt>
                  <dd>{config.max_duration_seconds / 3600} 小时</dd>
                </div>
              </dl>
              <h3>导出格式</h3>
              <p className="formats">JSON · SRT · VTT · TXT · MD</p>
              <button
                className="text-button"
                type="button"
                onClick={onSettings}
              >
                调整转录预设 <Icon name="arrow" size={15} />
              </button>
            </section>
          </aside>
        )}
      </div>
    </>
  );
}
