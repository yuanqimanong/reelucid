export const terminal = (s: string) =>
  ["succeeded", "partially_succeeded", "failed", "cancelled"].includes(s);
export const statuses: Record<string, string> = {
  queued: "排队中",
  running: "处理中",
  cancelling: "正在取消",
  cancelled: "已取消",
  succeeded: "已完成",
  partially_succeeded: "部分完成",
  failed: "处理失败",
};
export const stages: Record<string, string> = {
  queued: "等待处理",
  starting: "准备处理",
  wait_download: "等待下载资源",
  wait_asr: "等待转录资源",
  wait_transcode: "等待转码资源",
  wait_ai: "等待文稿分析资源",
  wait_install: "等待模型安装资源",
  load_model: "加载本地模型",
  acquire: "获取媒体",
  inspect: "检查媒体",
  normalize: "规范化音频",
  vad: "检测语音片段",
  asr: "本地语音转录",
  transcode: "媒体转码",
  export: "导出文稿",
  summarize: "生成摘要",
  qa: "查找证据",
  translate: "翻译文稿",
  extract: "提取信息",
  install_model: "安装模型",
  completed: "处理完成",
};
export const stageName = (s: string) => stages[s] ?? s;
export const clock = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 3600) ? `${Math.floor(s / 3600)}:` : ""}${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
export const bytes = (n: number) =>
  n > 1024 ** 3
    ? `${(n / 1024 ** 3).toFixed(1)} GB`
    : `${(n / 1024 ** 2).toFixed(1)} MB`;
export const date = (s: string) =>
  new Date(s).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
