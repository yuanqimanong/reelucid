import type { Settings } from "../../types";
import { Notice } from "../../components/ui";
import type { PendingCredentials } from "./saveSettings";

const adapterNames: Record<string, string> = {
  upload: "本地文件",
  http_media: "HTTP 媒体直链",
  yt_dlp: "yt-dlp 视频站点",
  bilibili: "Bilibili 公开 API（BV / av / 分 P / 短链）",
  dtk: "DTK v5 专用解析服务（需下方配置）",
  xhs: "小红书 XHS-Downloader",
};

type Props = {
  draft: Settings;
  savedXhs: Settings["xhs_resolver"];
  xhsLocalAvailable: boolean;
  credentials: string[];
  pending: PendingCredentials;
  onChange: (fields: Partial<Settings>) => void;
  onPendingChange: (fields: Partial<PendingCredentials>) => void;
  onDelete: (id: string) => void;
};

export function SourceSettings({
  draft,
  savedXhs,
  xhsLocalAvailable,
  credentials,
  pending,
  onChange,
  onPendingChange,
  onDelete,
}: Props) {
  function updateDtk(fields: Partial<Settings["source_resolver"]>) {
    onChange({ source_resolver: { ...draft.source_resolver, ...fields } });
  }
  function updateXhs(fields: Partial<Settings["xhs_resolver"]>) {
    onChange({ xhs_resolver: { ...draft.xhs_resolver, ...fields } });
  }
  return (
    <section className="card settings-section">
      <div className="section-title">
        <div>
          <h2>视频源与凭据</h2>
          <p>管理下载适配器及各站点使用的凭据。</p>
        </div>
      </div>
      <div className="adapter-list">
        {Object.entries(draft.adapters).map(([id, enabled]) => (
          <label className="toggle-line" key={id}>
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) =>
                onChange({
                  adapters: { ...draft.adapters, [id]: e.target.checked },
                })
              }
            />
            <strong>{adapterNames[id] ?? id}</strong>
          </label>
        ))}
      </div>
      <p>
        yt-dlp 覆盖
        YouTube、抖音、TikTok、小红书、微博、X、Twitch、Vimeo、腾讯视频、芒果
        TV、TED 等站点。支持目录不代表下载成功保证；登录、网络、风控与 DRM
        会影响可用性。
      </p>
      <a href="/api/v1/source-platforms" target="_blank" rel="noreferrer">
        查看当前版本的全部上游提取器目录
      </a>
      <label className="toggle-line">
        <input
          type="checkbox"
          checked={draft.source_resolver.enabled}
          onChange={(e) =>
            updateDtk({
              enabled: e.target.checked,
            })
          }
        />
        启用 DTK v5（抖音 / TikTok 自动路由；Bilibili 可通过 API 指定 dtk）
      </label>
      <label className="toggle-line">
        <input
          type="checkbox"
          checked={draft.source_resolver.download_session_bound}
          disabled={!draft.source_resolver.enabled}
          onChange={(e) =>
            updateDtk({
              download_session_bound: e.target.checked,
            })
          }
        />
        允许 DTK 下载需要原会话的 TikTok 视频
      </label>
      <p>
        启用后，DTK 会在自己的存储中保存这类视频，再由 Reelucid 取回。DTK 的 API
        Key 需要 <code>media:write</code> 发起下载和 <code>media:read</code>{" "}
        读取文件。
      </p>
      <label>
        自托管解析服务地址
        <input
          value={draft.source_resolver.base_url}
          placeholder="http://host.docker.internal:8080"
          onChange={(e) =>
            updateDtk({
              base_url: e.target.value,
            })
          }
        />
      </label>
      <label>
        解析服务 API Key 凭据名称
        <input
          value={draft.source_resolver.credential_ref ?? ""}
          placeholder="dtk-key"
          onChange={(e) =>
            updateDtk({
              credential_ref: e.target.value || null,
            })
          }
        />
      </label>
      <label>
        解析服务 API Key
        <input
          type="password"
          value={pending.dtkKey}
          autoComplete="new-password"
          placeholder="留空保留已有 Key；保存后加密存储"
          onChange={(e) => onPendingChange({ dtkKey: e.target.value })}
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={draft.xhs_resolver.enabled}
          disabled={
            !draft.xhs_resolver.enabled &&
            draft.xhs_resolver.mode === "local" &&
            !xhsLocalAvailable
          }
          onChange={(e) =>
            updateXhs({
              enabled: e.target.checked,
            })
          }
        />
        启用小红书专用解析（XHS-Downloader）
      </label>
      <label>
        小红书解析方式
        <select
          value={draft.xhs_resolver.mode}
          onChange={(e) => {
            const mode = e.target.value === "local" ? "local" : "remote";
            updateXhs({
              mode,
              base_url: mode === "remote" ? savedXhs.base_url : "",
              credential_ref:
                mode === "remote" ? savedXhs.credential_ref : null,
            });
          }}
        >
          <option value="local" disabled={!xhsLocalAvailable}>
            {xhsLocalAvailable ? "容器内插件（试用）" : "容器内插件（未安装）"}
          </option>
          <option value="remote">外部 XHS-Downloader 服务</option>
        </select>
      </label>
      {draft.xhs_resolver.mode === "remote" ? (
        <>
          <label>
            XHS-Downloader 服务地址
            <input
              value={draft.xhs_resolver.base_url}
              placeholder="http://xhs-resolver:5556"
              onChange={(e) =>
                updateXhs({
                  base_url: e.target.value,
                })
              }
            />
          </label>
          <p>
            仅连接你信任的解析服务。任务选用的小红书 Cookie 会发送给此服务。
          </p>
        </>
      ) : (
        !xhsLocalAvailable && (
          <Notice error>
            当前部署缺少小红书插件，请安装插件或选择外部服务。
          </Notice>
        )
      )}
      <label>
        新增站点凭据名称
        <input
          value={pending.cookieId}
          onChange={(e) => onPendingChange({ cookieId: e.target.value })}
          placeholder="bilibili"
        />
      </label>
      <label>
        Netscape cookies.txt 内容
        <textarea
          value={pending.cookie}
          onChange={(e) => onPendingChange({ cookie: e.target.value })}
          rows={3}
          placeholder="只在需要登录的站点配置。留空不保存。"
        />
      </label>
      <div className="credential-list">
        {credentials.map((id) => (
          <span key={id}>
            {id}
            <button aria-label={`删除凭据 ${id}`} onClick={() => onDelete(id)}>
              ×
            </button>
          </span>
        ))}
      </div>
    </section>
  );
}
