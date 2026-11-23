import type { Profile, Provider, Settings } from "../../types";
import { Icon } from "../../components/ui";

type Props = {
  draft: Settings;
  secret: string;
  onChange: (fields: Partial<Settings>) => void;
  onProfileChange: (fields: Partial<Profile>) => void;
  onKeyChange: (key: string) => void;
  onTest: (provider: string) => void;
};

export function AiSettings({
  draft,
  secret,
  onChange,
  onProfileChange,
  onKeyChange,
  onTest,
}: Props) {
  const profile = draft.processing_profiles[0];
  const provider = draft.providers[0] ?? {
    id: "default",
    name: "我的 AI 服务",
    protocol: "openai_compatible" as const,
    base_url: "",
    credential_ref: null,
    timeout_seconds: 120,
  };
  const binding = draft.model_bindings["text-default"] ?? {
    provider_id: provider.id,
    model: "",
    capabilities: ["text" as const],
  };

  function updateProvider(fields: Partial<Provider>) {
    onChange({
      providers: [{ ...provider, ...fields }, ...draft.providers.slice(1)],
    });
  }

  function setModel(model: string) {
    // The quick form owns the first profile and text-default; advanced bindings keep their IDs.
    onChange({
      model_bindings: {
        ...draft.model_bindings,
        "text-default": { ...binding, model },
      },
      processing_profiles: [
        { ...profile, text_binding_id: model ? "text-default" : null },
        ...draft.processing_profiles.slice(1),
      ],
    });
  }
  return (
    <section className="card settings-section">
      <div className="section-title">
        <div>
          <h2>AI 服务</h2>
          <p>可连接本地兼容服务或自选云端模型。</p>
        </div>
      </div>
      <label>
        服务名称
        <input
          value={provider.name}
          onChange={(e) => updateProvider({ name: e.target.value })}
        />
      </label>
      <label>
        API Base URL
        <input
          value={provider.base_url}
          onChange={(e) => updateProvider({ base_url: e.target.value })}
          placeholder="http://host.docker.internal:11434/v1"
        />
      </label>
      <div className="form-grid">
        <label>
          文本模型名称
          <input
            value={binding.model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="已安装或服务商提供的模型名称"
          />
        </label>
        <label>
          API Key
          <input
            type="password"
            value={secret}
            onChange={(e) => onKeyChange(e.target.value)}
            autoComplete="new-password"
            placeholder={
              provider.credential_ref
                ? "已配置；留空保留原值"
                : "本地服务可留空"
            }
          />
        </label>
      </div>
      <label className="check-line">
        <input
          type="checkbox"
          checked={profile.allow_remote_ai}
          onChange={(e) =>
            onProfileChange({ allow_remote_ai: e.target.checked })
          }
        />
        允许此预设将文稿发送到远程 AI 服务
      </label>
      <div className="help small">
        <Icon name="lock" size={16} />
        <span>
          Key 在服务端加密保存。选择远程服务后，相关文稿将发送给该服务。
        </span>
      </div>
      <button className="secondary" onClick={() => onTest(provider.id)}>
        测试已保存的连接
      </button>
    </section>
  );
}
