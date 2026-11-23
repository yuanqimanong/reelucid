import { useState, useRef } from "react";
import type { Settings } from "../types";
import { Icon, Badge, JobProgress, Notice } from "../components/ui";
import { terminal, date, stageName } from "../format";
import { api } from "../api";
import { useJobDetail } from "./task/useJobDetail";
import { TranscriptReader } from "./task/TranscriptReader";
import { TaskAnswers } from "./task/TaskAnswers";
export function TaskDetail({
  id,
  config,
  onBack,
  onOpen,
}: {
  id: string;
  config: Settings;
  onBack: () => void;
  onOpen: (id: string) => void;
}) {
  const {
    job,
    asset,
    result,
    transcript,
    transcriptArtifactId,
    error,
    setError,
    act,
  } = useJobDetail(id);
  const name = asset?.name ?? "音视频任务";
  const media = useRef<HTMLMediaElement | null>(null);
  const [time, setTime] = useState(0);
  const seek = (ms: number) => {
    if (media.current) {
      media.current.currentTime = ms / 1000;
      void media.current.play().catch(() => {});
    }
    setTime(ms);
  };
  if (!job)
    return (
      <div className="empty">
        正在读取任务…{error && <Notice error>{error}</Notice>}
      </div>
    );
  return (
    <>
      <button className="back-button" onClick={onBack}>
        <Icon name="back" size={17} />
        返回任务列表
      </button>
      <div className="page-heading detail-heading">
        <div>
          <span className="eyebrow">
            {job.job_id.slice(-12)} · 第 {job.attempt} 次处理
          </span>
          <h1>{job.kind === "model" ? "本地模型安装" : name}</h1>
          <div className="heading-meta">
            <Badge status={job.status} />
            <span>{date(job.created_at)}</span>
            <span>{stageName(job.current_stage)}</span>
          </div>
        </div>
        <div className="actions">
          {!terminal(job.status) && (
            <button className="secondary" onClick={() => void act("cancel")}>
              取消任务
            </button>
          )}
          {["failed", "cancelled", "partially_succeeded"].includes(
            job.status,
          ) && (
            <button className="secondary" onClick={() => void act("retry")}>
              重新处理任务
            </button>
          )}
          {terminal(job.status) && (
            <button
              className="text-button muted"
              onClick={async () => {
                if (confirm("删除这条任务记录？历史文件暂不自动清理。")) {
                  try {
                    await api(`/jobs/${id}`, { method: "DELETE" });
                    onBack();
                  } catch (e) {
                    setError((e as Error).message);
                  }
                }
              }}
            >
              删除记录
            </button>
          )}
        </div>
      </div>
      {error && <Notice error>{error}</Notice>}
      {job.error && (
        <Notice error>
          {job.error.message} <small>{job.error.code}</small>
        </Notice>
      )}
      {job.warnings.length > 0 && (
        <Notice>处理提示：{job.warnings.join(" · ")}</Notice>
      )}
      {!terminal(job.status) && (
        <div className="card processing">
          <span className="pulse" />
          <div>
            <strong>{stageName(job.current_stage)}</strong>
            <small>可以关闭页面，任务会继续在服务端处理。</small>
          </div>
          <JobProgress job={job} />
        </div>
      )}
      {job.kind === "model" ? (
        <div className="card empty">
          <Icon name="download" size={36} />
          <h3>
            {job.status === "succeeded" ? "模型已安装" : "本地模型安装任务"}
          </h3>
          <p>安装完成后，在模型与设置中选择默认转录模型并保存。</p>
        </div>
      ) : (
        <div className="reader-layout">
          <TranscriptReader
            assetId={job.asset_id}
            name={name}
            hasVideo={!!asset?.media.has_video}
            transcript={transcript}
            media={media}
            time={time}
            setTime={setTime}
            seek={seek}
          />
          <aside className="reader-aside">
            <section className="card">
              <div className="aside-title">
                <Icon name="download" size={18} />
                <h2>导出结果</h2>
              </div>
              <div className="export-links">
                {(job.artifacts ?? [])
                  .filter((a) =>
                    ["subtitle", "document", "transcript"].includes(a.kind),
                  )
                  .map((a) => (
                    <a key={a.artifact_id} href={a.content_url} download>
                      <span>{a.format.toUpperCase()}</span>
                      <Icon name="download" size={15} />
                    </a>
                  ))}
              </div>
              {!job.artifacts?.length && (
                <p className="muted small">产物完成后即可下载。</p>
              )}
            </section>
            <TaskAnswers
              result={result}
              transcriptArtifactId={transcriptArtifactId}
              assetId={job.asset_id}
              profiles={config.processing_profiles}
              seek={seek}
              onOpen={onOpen}
              setError={setError}
            />
            <section className="card stage-card">
              <h2>处理轨迹</h2>
              {job.stages?.map((s) => (
                <div key={s.name}>
                  <i className={s.status === "succeeded" ? "done" : ""} />
                  <span>{stageName(s.name)}</span>
                  <small>
                    {s.status === "succeeded"
                      ? "完成"
                      : s.status === "running"
                        ? "进行中"
                        : s.status === "failed"
                          ? "失败"
                          : s.status === "cancelled"
                            ? "已取消"
                            : "等待"}
                  </small>
                </div>
              ))}
            </section>
          </aside>
        </div>
      )}
    </>
  );
}
