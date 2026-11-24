# Reelucid API 功能清单

下表的路径均以 `/api/v1` 为前缀。**管理员**表示 `Authorization: Bearer <admin_token>` 或有效的 Web 会话 Cookie。使用 Cookie 提交写请求时需带同源 `Origin`。Fin 等服务端调用方可直接用 Bearer。接口尚无独立的转码权限或租户隔离，请把管理员令牌保存在可信服务端。[OpenAPI](openapi.json) · [部署与配置](../README.md)

| 功能 | Web 入口 | HTTP 方法和路径 | 鉴权 | 所需配置或前提 |
| --- | --- | --- | --- | --- |
| 存活和就绪 | 部署健康检查 | `GET /health/live`、`GET /health/ready` | 公开 | 就绪检查覆盖 API 的后台任务生命周期。 |
| 登录、退出 | 登录页、退出工作台 | `POST /auth/session`、`DELETE /auth/session` | 登录请求体含管理员 `token`。退出需管理员会话或 Bearer | 启动时生成或显式注入管理员令牌。 |
| 发现能力 | 工作台初始化 | `GET /capabilities` | 管理员 | 返回模型状态、操作、转码预设、调度与适配器状态。 |
| 查询支持的来源 | API | `GET /source-platforms` | 管理员 | 列出 yt-dlp 提取器及上游维护标记。单条 URL 的可用性在下载时确定。 |
| 上传本地音视频 | 新建任务 → 本地文件 | `POST /assets` | 管理员 | `multipart/form-data` 的 `file`。启用 `adapters.upload`，遵守上传上限。返回 `asset_id`。 |
| URL 导入 | 新建任务 → 视频链接 | `POST /jobs` | 管理员 | `source.kind=url`，填 URL 或含一条链接的分享文案。关闭离线模式，并启用适配器。DTK、XHS 和站点凭据按来源配置。请求同时建立素材和处理任务。 |
| 查看素材、播放或读取原片 | 任务详情 → 播放器 | `GET /assets/{id}`、`GET /assets/{id}/media` | 管理员 | 已上传或已完成来源下载。媒体支持 HTTP Range。 |
| 本地语音转录 | 新建任务 → 语音转录 | `POST /jobs`，`operations[].type=transcribe` | 管理员 | 处理预设选用已安装的 ASR 模型。原片须有音轨。可设置语言与导出格式。省略 `operations` 时默认转录。 |
| 媒体转码 | 新建任务 → 媒体转码 | `POST /jobs`，`operations[].type=transcode` | 管理员 | `options.preset` 取 `audio_mp3`、`audio_wav` 或 `video_mp4`。需要 FFmpeg、ffprobe 及对应编码器。音频预设须有音轨，MP4 预设须有视频轨。可单独使用或和转录一起提交。 |
| 列出和查看任务 | 任务与文稿、任务详情 | `GET /jobs`、`GET /jobs/{id}` | 管理员 | 列表支持 `limit`、`cursor`、`status`、`created_before`、`created_after`。详情含进度、阶段、错误及已发布产物。 |
| 订阅任务状态 | 任务详情实时刷新 | `GET /jobs/{id}/events` | 管理员 | SSE，使用 `Last-Event-ID` 恢复。游标过期时重新读取任务详情后订阅。 |
| 取消、重试或删除任务 | 任务详情 → 操作 | `POST /jobs/{id}/cancel`、`POST /jobs/{id}/retry`、`DELETE /jobs/{id}` | 管理员 | 失败、取消或部分成功可重试。终态任务可软删除，文件仍保存在数据卷。 |
| 查看文稿和操作结果 | 任务详情 → 同步文稿、AI 结果 | `GET /jobs/{id}/result`，可加 `?include=transcript` | 管理员 | 任务须到终态。转录结果有文稿版本，纯转码结果含媒体产物。使用查询参数可读取文稿段落。 |
| 摘要、翻译、提取及问答 | 新建任务有摘要，文稿详情有问答。翻译和提取通过 API 提交 | `POST /jobs`，`operations[].type` 取 `summarize`、`translate`、`extract` 或 `qa`，结果从 `GET /jobs/{id}/result` 读取 | 管理员 | 配置兼容 `chat/completions` 的 Provider、模型绑定和处理预设。远程服务还需预设允许远程 AI 且实例未开启离线模式。`qa` 需要 `options.question`。可通过 `source.transcript_artifact_id` 指定素材的已有文稿。 |
| 文稿与字幕导出 | 任务详情 → 导出结果 | `POST /jobs` 指定 `options.export_formats`。`GET /jobs/{id}/result` 获取产物 | 管理员 | 支持 JSON、SRT、VTT、TXT 和 Markdown。已有文稿的文本任务也可导出对应格式。 |
| 查询、下载产物 | 任务详情 → 导出结果 | `GET /artifacts/{id}`、`GET /artifacts/{id}/content` | 管理员 | 产物就绪后通过 `content_url` 下载，支持 HTTP Range。转码产物为 `kind=media`，元数据提供 `format`、`size_bytes` 和 `sha256`。 |
| 读取或保存实例设置 | 模型与设置 | `GET /admin/system-config`、`PUT /admin/system-config` | 管理员 | 保存须提交当前 `revision`。设置涵盖并发与资源约束、模型和 AI、来源适配器及解析服务。 |
| 管理模型 | 模型与设置 → 本地模型 | `GET /admin/asr-models`、`POST /admin/asr-models/{id}/install` | 管理员 | 模型安装返回可跟踪的任务，需要联网下载和可写模型目录。默认 Docker 镜像已预装 `small`。 |
| 写入或删除凭据 | 模型与设置 → 视频源与凭据、AI 服务 | `PUT /admin/credentials/{id}`、`DELETE /admin/credentials/{id}` | 管理员 | 凭据写入后在数据卷加密保存。系统配置只返回凭据标识。 |
| 测试文本服务 | 模型与设置 → AI 服务 | `POST /admin/providers/{id}/test` | 管理员 | 已保存 Provider 及至少一个模型绑定，服务可达且所需凭据已保存。 |
| 查看或安装可选组件 | 组件页、宿主组件代理 | `GET /admin/components`、`POST /admin/components/{component}/install`、`POST /admin/components/agent/poll` | 管理员。代理轮询只接受 Bearer | Web 安装需宿主运行 `reelucid components agent`。支持 `dtk` 和 `xhs`。 |

## 仍在计划中的功能

OCR、视觉分析、来源字幕复用、任务完成回调、自动文件回收、直播和跨主机 worker 尚未实现。当前任务状态订阅使用 SSE，`capabilities.visual` 为 `false`。
