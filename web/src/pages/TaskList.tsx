import { useState } from "react";
import type { Job } from "../types";
import { Icon, Badge, JobProgress } from "../components/ui";
import { terminal, date } from "../format";
import { api } from "../api";
export function TaskList({
  jobs: recent,
  nextCursor,
  onOpen,
  onNew,
}: {
  jobs: Job[];
  nextCursor: number | null;
  onOpen: (id: string) => void;
  onNew: () => void;
}) {
  const [filter, setFilter] = useState("all");
  const [older, setOlder] = useState<Job[]>([]);
  const [after, setAfter] = useState<number | null | undefined>(undefined);
  const [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const cursor = after === undefined ? nextCursor : after;
  const jobs = [
    ...new Map([...older, ...recent].map((job) => [job.job_id, job])).values(),
  ].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const filtered = jobs.filter(
    (j) =>
      filter === "all" ||
      (filter === "active"
        ? !terminal(j.status)
        : filter === "done"
          ? ["succeeded", "partially_succeeded"].includes(j.status)
          : j.status === "failed"),
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>任务与文稿</h1>
          <p>查看处理进度，继续阅读已经整理好的内容。</p>
        </div>
        <button className="primary" onClick={onNew}>
          <Icon name="plus" size={18} />
          新建任务
        </button>
      </div>
      <div className="stats">
        {[
          ["已加载任务", jobs.length],
          ["处理中", jobs.filter((j) => !terminal(j.status)).length],
          ["已完成", jobs.filter((j) => j.status === "succeeded").length],
        ].map(([label, count]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{count}</strong>
          </div>
        ))}
      </div>
      {error && <p role="alert">{error}</p>}
      {cursor !== null && (
        <button
          className="secondary"
          disabled={loading}
          onClick={async () => {
            setLoading(true);
            setError("");
            try {
              const page = await api<{
                items: Job[];
                next_cursor: number | null;
              }>(`/jobs?limit=100&cursor=${cursor}`);
              setOlder((items) => [...items, ...page.items]);
              setAfter(page.next_cursor);
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setLoading(false);
            }
          }}
        >
          {loading ? "正在读取…" : "加载更早的任务"}
        </button>
      )}
      <div className="card">
        <div className="list-toolbar">
          <div className="filter-tabs">
            {[
              ["all", "全部"],
              ["active", "进行中"],
              ["done", "已完成"],
              ["failed", "失败"],
            ].map(([value, label]) => (
              <button
                className={filter === value ? "active" : ""}
                aria-pressed={filter === value}
                key={value}
                onClick={() => setFilter(value)}
              >
                {label}
              </button>
            ))}
          </div>
          <small>{filtered.length} 项内容</small>
        </div>
        {filtered.length ? (
          <div className="task-table">
            {filtered.map((job) => (
              <button
                className="task-row"
                key={job.job_id}
                onClick={() => onOpen(job.job_id)}
              >
                <span
                  className={`file-tile ${job.kind === "model" ? "model-tile" : ""}`}
                >
                  <Icon name={job.kind === "model" ? "download" : "play"} />
                </span>
                <div className="task-name">
                  <strong>
                    {job.kind === "model"
                      ? "安装本地转录模型"
                      : `音视频任务 · ${job.job_id.slice(-8)}`}
                  </strong>
                  <small>
                    {date(job.created_at)} · 第 {job.attempt} 次处理
                  </small>
                </div>
                <div className="row-progress">
                  {!terminal(job.status) ? (
                    <JobProgress job={job} />
                  ) : (
                    <small>{job.error?.message || "点击查看结果与产物"}</small>
                  )}
                </div>
                <Badge status={job.status} />
                <Icon name="arrow" size={17} />
              </button>
            ))}
          </div>
        ) : (
          <div className="empty">
            <Icon name="tasks" size={36} />
            <h3>{jobs.length ? "当前筛选下没有任务" : "还没有任务"}</h3>
            {jobs.length ? (
              <button className="secondary" onClick={() => setFilter("all")}>
                查看全部任务
              </button>
            ) : (
              <button className="secondary" onClick={onNew}>
                添加第一段内容
              </button>
            )}
          </div>
        )}
      </div>
    </>
  );
}
