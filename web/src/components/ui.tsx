import type React from "react";
import type { Job } from "../types";
import { terminal, statuses, clock, bytes, stageName } from "../format";
export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    plus: <path d="M12 5v14M5 12h14" />,
    tasks: (
      <>
        <rect x="4" y="3" width="16" height="18" rx="3" />
        <path d="m8 9 1 1 2-2m2 1h3m-8 7 1 1 2-2m2 1h3" />
      </>
    ),
    settings: (
      <>
        <path d="M4 7h16M4 17h16" />
        <circle cx="9" cy="7" r="3" />
        <circle cx="15" cy="17" r="3" />
      </>
    ),
    components: (
      <>
        <path d="m12 3 8 4.5v9L12 21l-8-4.5v-9Zm-8 4.5 8 4.5 8-4.5M12 12v9M8 5.2l8 4.6" />
      </>
    ),
    copy: (
      <>
        <rect x="8" y="8" width="12" height="13" rx="2" />
        <path d="M16 8V3H4v13h4" />
      </>
    ),
    upload: (
      <>
        <path d="m8 8 4-4 4 4M12 4v12M4 15v5h16v-5" />
      </>
    ),
    link: (
      <>
        <path d="m10 13 4-4m-5 7-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m0 1 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" />
      </>
    ),
    arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
    play: <path d="m9 5 11 7-11 7Z" />,
    download: <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />,
    check: <path d="m5 12 4 4 10-10" />,
    search: (
      <>
        <circle cx="10.5" cy="10.5" r="6.5" />
        <path d="m16 16 5 5" />
      </>
    ),
    spark: (
      <path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4" />
    ),
    back: <path d="M20 12H4m6-6-6 6 6 6" />,
    lock: (
      <>
        <rect x="5" y="10" width="14" height="11" rx="2" />
        <path d="M8 10V6a4 4 0 0 1 8 0v4m-4 4v3" />
      </>
    ),
    logout: (
      <>
        <path d="M9 4H4v16h5m5-12 4 4-4 4m-6-4h10" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] ?? paths.tasks}
    </svg>
  );
}
export function Brand() {
  return (
    <div className="brand">
      <div className="brandmark" aria-hidden="true">
        <span>帧</span>
      </div>
      <div>
        <strong>帧闻录</strong>
        <small>Reelucid</small>
      </div>
    </div>
  );
}
export function Badge({ status }: { status: string }) {
  return (
    <span className={`badge ${status}`}>
      <i />
      {statuses[status] ?? status}
    </span>
  );
}
export function Notice({
  children,
  error = false,
}: {
  children: React.ReactNode;
  error?: boolean;
}) {
  return (
    <div
      role={error ? "alert" : "status"}
      className={`notice ${error ? "error" : ""}`}
    >
      {children}
    </div>
  );
}
export function JobProgress({ job }: { job: Job }) {
  const p = job.progress;
  const ratio = terminal(job.status)
    ? job.status === "succeeded"
      ? 100
      : 0
    : p.total_ms
      ? Math.min(100, (100 * (p.completed_ms ?? 0)) / p.total_ms)
      : p.total_bytes
        ? Math.min(100, (100 * (p.completed_bytes ?? 0)) / p.total_bytes)
        : undefined;
  return (
    <div className="progress-wrap">
      <div className="progress">
        <i
          className={ratio === undefined ? "indeterminate" : ""}
          style={{ width: `${ratio ?? 30}%` }}
        />
      </div>
      <span>
        {p.total_ms
          ? `${clock(p.completed_ms ?? 0)} / ${clock(p.total_ms)}`
          : p.completed_bytes
            ? bytes(p.completed_bytes)
            : stageName(job.current_stage)}
      </span>
    </div>
  );
}
