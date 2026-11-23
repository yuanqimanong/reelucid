import { api, json } from "../../api";
import type { Settings } from "../../types";

export type PendingCredentials = {
  providerKey: string;
  dtkKey: string;
  cookieId: string;
  cookie: string;
};

type SecretField = "providerKey" | "dtkKey" | "cookie";

// A credential PUT advances the same revision checked by the final config PUT.
// Publish each acknowledged change so a failed save can be retried from that revision.
export async function saveSettings(
  settings: Settings,
  pending: PendingCredentials,
  onCredentialSaved: (field: SecretField, settings: Settings) => void,
) {
  if (
    !settings ||
    !Number.isSafeInteger(settings.revision) ||
    settings.revision < 1
  ) {
    throw new Error("配置 JSON 需要包含有效的 revision，才能保存本次修改。");
  }
  let value = structuredClone(settings);
  async function write(
    id: string,
    field: SecretField,
    changes: Partial<Settings>,
  ) {
    await api(
      `/admin/credentials/${encodeURIComponent(id)}`,
      json({ value: pending[field] }, "PUT"),
    );
    value = { ...value, ...changes, revision: value.revision + 1 };
    onCredentialSaved(field, value);
  }
  if (pending.providerKey) {
    const provider = value.providers?.[0];
    if (!provider?.base_url)
      throw new Error("先填写 AI 服务地址，再保存 API Key。");
    const id = `provider-${provider.id}`;
    await write(id, "providerKey", {
      providers: [
        { ...provider, credential_ref: id },
        ...value.providers.slice(1),
      ],
    });
  }
  if (pending.cookie) await write(pending.cookieId, "cookie", {});
  if (pending.dtkKey)
    await write("resolver-dtk", "dtkKey", {
      source_resolver: {
        ...value.source_resolver,
        credential_ref: "resolver-dtk",
      },
    });
  const response = await api<{ config: Settings }>(
    "/admin/system-config",
    json(value, "PUT"),
  );
  return response.config;
}
