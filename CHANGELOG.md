# 更新记录

## 1.1.4

- PYP 可用管理员 Bearer Token 按任务 UUID 回收 Reelucid 的任务记录、产物和工作目录。仍有任务引用的原件保留，清理可重复调用。
- 新增同机部署的 `compose.pyp.yaml`，将 Reelucid 产物集中到 `/data/apps/pyp_data/reelucid/artifacts`。
- 服务启动时升级 SQLite 事件表，物理删除任务后事件游标继续递增。

## 1.1.2

相对于 `v1.1.1`，本版本包含 [PR #2](https://github.com/yuanqimanong/reelucid/pull/2) 的两项功能变更，以及版本同步和发布验收改进。

### 新增

- 管理员可通过 `GET /api/v1/assets/{identifier}/source-url` 读取 URL 素材加密保存的来源链接（使用素材 ID，而非任务 ID）。成功响应为 `{"url":"…"}`，含 `Cache-Control: private, no-store`。下载失败的 URL 素材也可查询；非 URL 素材或不存在的素材返回 404，未认证返回 401。普通素材详情继续隐藏完整来源 URL。
- 来源服务器返回 HTTP 412 时，下载错误独立分类为 `SOURCE_PRECONDITION_FAILED`，`retryable=false`。该分类不表示 Reelucid 的查询接口返回 HTTP 412；任务详情与结果保留明确错误码。既有 HTTP 401、403、404、429、5xx 分类不变。

### 发布和验证

- Python 项目、包版本、`uv.lock` 项目条目、Web 包及锁文件、OpenAPI、Compose 默认镜像标签统一为 1.1.2，并新增 OCI 镜像版本标签和自动一致性检查。
- 分支/PR 镜像、发布推送前的本地镜像及推送后从 Docker Hub 拉取的镜像运行同一套 HTTP 验收。新增管理员认证、原始链接保密和禁止缓存、真实 HTTP 412 的任务错误、幂等、保留数据卷重建容器等检查，并保留原有 FFmpeg 转码和本地 ASR 验收。
- 新增[来源 URL API 示例](docs/source-url-api.md)。测试代理仅用于验收，不是生产代理，也不访问任何外部站点。

### 升级说明

- PR #2 读取既有加密来源记录，未引入新的数据库迁移或配置必填项。备份并保留原有 `/data` 卷及 `secrets/master_key`；否则旧 URL 和凭据无法解密。升级前备份 SQLite、媒体、产物和密钥。
- URL 可能含签名和访问令牌；仅在受信任的管理员环境中查询，不得写入普通日志或共享缓存。
- 如需回滚，将 `REELUCID_IMAGE` 改回 `yuanqimanong/reelucid:1.1.1`，再执行 `docker compose pull reelucid` 和 `docker compose up -d --no-build --wait reelucid`。保留原有数据卷和密钥。
