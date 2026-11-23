import { useMemo, useState } from "react";
import { api, json } from "../../api";
import { Icon } from "../../components/ui";
import { clock } from "../../format";
import type { Job, Result, Profile } from "../../types";
type Props = {
  result: Result | null;
  transcriptArtifactId: string | undefined;
  assetId: string | null;
  profiles: Profile[];
  seek: (ms: number) => void;
  onOpen: (id: string) => void;
  setError: (message: string) => void;
};
export function TaskAnswers({
  result,
  transcriptArtifactId,
  assetId,
  profiles,
  seek,
  onOpen,
  setError,
}: Props) {
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [profile, setProfile] = useState(profiles[0]?.id ?? "local-default");
  const claims = result?.outputs?.flatMap((o) => o.claims ?? []) ?? [];
  const evidenceById = useMemo(
    () => new Map(result?.evidence?.map((e) => [e.evidence_id, e])),
    [result?.evidence],
  );
  return (
    <section className="card">
      <div className="aside-title">
        <Icon name="spark" size={19} />
        <h2>AI 整理与问答</h2>
      </div>
      {claims.length ? (
        <div className="claims">
          {claims.map((claim, index) => (
            <div key={index}>
              <p>{claim.text}</p>
              <div>
                {claim.evidence_ids.map((ref) => {
                  const evidence = evidenceById.get(ref);
                  return (
                    evidence && (
                      <button
                        className="citation"
                        key={ref}
                        onClick={() => seek(evidence.start_ms)}
                      >
                        {clock(evidence.start_ms)} ↗
                      </button>
                    )
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="muted small">
          对已完成的文稿提问，答案附原话时间。需要先配置文本模型。
        </p>
      )}
      {assetId && transcriptArtifactId && (
        <form
          className="ask-form"
          onSubmit={async (e) => {
            e.preventDefault();
            setAsking(true);
            setError("");
            try {
              const task = await api<Job>(
                "/jobs",
                json({
                  source: {
                    kind: "asset",
                    asset_id: assetId,
                    transcript_artifact_id: transcriptArtifactId,
                  },
                  processing_profile_id: profile,
                  operations: [
                    {
                      id: "answer",
                      type: "qa",
                      required: true,
                      options: { question },
                    },
                  ],
                }),
              );
              onOpen(task.job_id);
            } catch (err) {
              setError((err as Error).message);
            } finally {
              setAsking(false);
            }
          }}
        >
          <label>
            AI 预设
            <select
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
            >
              {profiles.map((p) => (
                <option key={p.id}>{p.id}</option>
              ))}
            </select>
          </label>
          <textarea
            aria-label="针对当前视频提问"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="这段内容的主要结论是什么？"
            rows={3}
            required
            maxLength={2000}
          />
          <button className="primary wide" disabled={asking}>
            {asking ? "正在提交…" : "带着出处回答"}
            <Icon name="arrow" size={16} />
          </button>
        </form>
      )}
    </section>
  );
}
