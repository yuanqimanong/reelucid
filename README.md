# Reelucid · 帧闻录

Reelucid 是一款可自行部署的音视频转录和转码工具。你可以上传本地文件，也可以提交视频 URL，让 Reelucid 下载媒体。语音在本地识别，生成的文稿和字幕带有原片时间戳。网页支持按文稿跳转播放、下载转码文件和配置独立的文本 AI 服务。

可选文本任务包括摘要、翻译、提取和问答。OCR、视觉分析、来源字幕复用、完成回调、自动文件回收、直播和跨主机 worker 仍待实现。[API 功能清单](docs/api-coverage.md) · [OpenAPI](docs/openapi.json)

## Docker 启动

使用 Docker Engine 和 Compose（Linux amd64）。建议分配 4 核、6 GiB 内存，并留足磁盘空间。构建镜像时会联网下载依赖和固定版本的 Whisper `small` 权重，并校验模型文件。镜像构建完成后即可离线转录。

```bash
docker compose up -d --build
docker compose exec reelucid cat /data/secrets/admin_token
```

打开 <http://127.0.0.1:8000>，用上面读取的管理员 Token 登录。部署时若已指定 Token，用指定的值登录。

默认部署只启动一个 `reelucid` 容器。SQLite、原片、产物、凭据和另行安装的模型保存在 `reelucid-data` 卷。镜像预装 Whisper `small`，使用 CPU INT8 推理。DTK 及其数据库、Redis 和远程小红书服务可按需安装。

### 从 Docker Hub 拉取镜像

在项目根目录的 `.env` 中设置 `REELUCID_IMAGE=yuanqimanong/reelucid:1.1.0`。使用自己的私有仓库时，先在部署机器上运行 `docker login`。

```bash
docker compose pull reelucid
docker compose up -d --no-build --wait reelucid
```

### 发布到 Docker Hub

1. 在个人命名空间下创建 `reelucid` 镜像仓库，再创建权限为 `Read & Write` 的个人访问令牌。[创建仓库](https://docs.docker.com/docker-hub/repos/create/) · [创建令牌](https://docs.docker.com/security/access-tokens/)
2. 打开 GitHub 仓库的 `Settings → Secrets and variables → Actions`。在 `Variables` 添加 `DOCKERHUB_USERNAME`，值为 Docker Hub 用户名。在 `Secrets` 添加 `DOCKERHUB_TOKEN`，值为个人访问令牌。
3. 将工作流提交推送到 GitHub，在待发布提交上创建并推送与 `pyproject.toml` 版本一致的 Git 标签。例如，发布 `1.1.0` 时运行以下命令。

   ```bash
   git tag v1.1.0
   git push origin v1.1.0
   ```

GitHub Actions 在 Python、前端和容器就绪检查通过后推送 `你的DockerID/reelucid:1.1.0`。发布目标为 `linux/amd64`，镜像标签取自项目版本。[发布工作流](.github/workflows/ci.yml)

### Web 使用

1. 打开「新建任务」，上传音视频或粘贴一条视频链接。语音转录可用默认预设。需要转换文件时选择「媒体转码」，关闭「语音转录」即可单独转码。
2. 在「任务与文稿」查看进度。打开任务详情，点击文稿时间戳跳到原片对应位置，或下载文稿、字幕及 MP3、WAV、MP4 转码产物。
3. 在「模型与设置」管理转录预设、下载凭据和文本 AI，也可以安装其他本地模型。

## 可选组件

在源码目录打开宿主机终端。可以直接运行安装命令，也可以启动宿主代理，从网页上的「组件」页安装。

```bash
uv run --locked --extra server reelucid components list
uv run --locked --extra server reelucid components install xhs
uv run --locked --extra server reelucid components install dtk
uv run --locked --extra server reelucid components agent
```

从网页安装时，让 `components agent` 保持运行，由它接收安装请求。如果修改过 Compose 项目名，在组件命令中加上 `--core-project <name>`。

| 组件 | 宿主安装后启动的服务 | Reelucid 容器内地址 |
| --- | --- | --- |
| DTK 5.1.13 | API、worker、downloader、PostgreSQL、Redis，另有一次性数据库迁移容器 | `http://reelucid-managed-dtk:8000` |
| XHS-Downloader 2.8 | `xhs-resolver` | `http://xhs-resolver:5556` |

在「模型与设置 → 视频源与凭据」启用所需服务并填写地址。首次使用 DTK 时，先在宿主机打开 `http://127.0.0.1:18080`，完成初始化并签发 API Key。如果端口已被占用，可以在 `components install dtk` 或 `components agent` 中指定 `--dtk-port`。如果已有解析服务，填写 Reelucid 容器能访问的地址。

## API 使用

下面是 Bash 示例。Windows PowerShell 可通过 `$env:REELUCID_TOKEN = (docker compose exec -T reelucid cat /data/secrets/admin_token).Trim()` 读取 Token，并使用 `curl.exe` 或实例的 `/docs` 页面调用接口。

```bash
export REELUCID_TOKEN="$(docker compose exec -T reelucid cat /data/secrets/admin_token)"

# 1. 上传，记录返回的 asset_id
curl -fsS -H "Authorization: Bearer $REELUCID_TOKEN" \
  -F file=@demo.mp4 http://localhost:8000/api/v1/assets

# 2. 替换 ast_example；202 返回 job_id
curl -fsS -H "Authorization: Bearer $REELUCID_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-001' \
  -d '{"source":{"kind":"asset","asset_id":"ast_example"},"operations":[{"id":"transcript","type":"transcribe"}]}' \
  http://localhost:8000/api/v1/jobs

# URL 输入使用相同接口
curl -fsS -H "Authorization: Bearer $REELUCID_TOKEN" -H 'Content-Type: application/json' \
  -d '{"source":{"kind":"url","url":"https://example.com/demo.mp4"}}' \
  http://localhost:8000/api/v1/jobs

# 3. 查询、订阅、读取结果；替换 job_example
curl -fsS -H "Authorization: Bearer $REELUCID_TOKEN" http://localhost:8000/api/v1/jobs/job_example
curl -N -H "Authorization: Bearer $REELUCID_TOKEN" http://localhost:8000/api/v1/jobs/job_example/events
curl -fsS -H "Authorization: Bearer $REELUCID_TOKEN" \
  'http://localhost:8000/api/v1/jobs/job_example/result?include=transcript'
```

结果默认返回文稿版本信息和下载链接。加上 `include=transcript` 可获取完整文稿，时间戳用原片上的整数毫秒表示。下载时，用同一个 Bearer Token 请求结果中的 `content_url`。原片和产物都可以用 HTTP Range 读取。幂等键保留 24 小时。用同一个键提交不同的请求会收到 409。

### 媒体转码

`GET /api/v1/capabilities` 的 `operations` 和 `transcode_presets` 可以查询转码是否可用。单独转码只需 FFmpeg 和 ffprobe，可使用上传素材或 URL。向 `POST /api/v1/jobs` 提交以下 JSON，将 `ast_example` 替换为实际 `asset_id`。

```json
{
  "source": {"kind": "asset", "asset_id": "ast_example"},
  "operations": [{"id": "media", "type": "transcode", "options": {"preset": "audio_mp3"}}]
}
```

可用预设为 `audio_mp3`（MP3）、`audio_wav`（PCM WAV）和 `video_mp4`（H.264 视频，有音轨时编码为 AAC）。在 `operations` 中再加 `{"id":"transcript","type":"transcribe"}` 可同时生成文稿、字幕和转码文件，此时需要已安装的 ASR 模型。成功的转码在结果 `artifacts` 中返回 `kind: "media"`、`artifact_id`、`format`、`size_bytes`、`sha256` 和 `content_url`。若同一任务有多项转码，可通过 `outputs[].artifact_id` 找到各自的产物。

### 任务与文本结果

提交 URL 时，服务在同一个事务里创建媒体记录（`asset_id`）和任务。队列满时返回 429，稍后可用同一个幂等键重试。处理本地文件时，先用 `POST /assets` 上传，再用返回的 `asset_id` 创建任务。同一文件的 ID 可以复用。

只执行文本操作且 `source.kind=asset` 时，可用 `source.transcript_artifact_id` 指定使用哪份文稿。这个值是原任务产物中 `kind=transcript` 那一项的 `artifact_id`。从网页上的文稿发起提问时，页面会自动传入该值。重试沿用提交时选中的版本。如果任务读取前文稿已被删除，任务会报错。

用 `POST /api/v1/jobs/{id}/cancel` 取消任务。Reelucid 会关闭正在等待的 AI 请求，停止后续批次和格式重试。已发出的请求是否继续处理或计费，取决于模型服务商。本地推理在当前分块结束时响应取消，视频下载在适配器检查取消信号时退出，媒体转码会终止正在运行的 FFmpeg。取消模型安装仍要等当前同步下载结束。如果取消发生在模型发布前，文件会留在暂存目录。

用 `POST /api/v1/jobs/{id}/retry` 重试失败、已取消或部分完成的任务。重试会复用已登记且哈希匹配的转录块，并重新执行请求中的转码和 AI 操作。

## 视频源与扩展

- 本地文件、HTTP 媒体直链、`yt-dlp` 及 Bilibili 公开 API 下载的媒体都走同一套转录流程。音视频分轨时可以分开下载，再在本地合并。网页和 API 都支持自动选择或手动指定下载方式，也接受只含一条 URL 的分享文案。
- 小红书建议提交单条作品的完整分享链接。远程 XHS-Downloader 可按上节安装。也可以用 `docker build --target with-xhs -t reelucid:with-xhs .` 构建内置解析镜像，将 `REELUCID_IMAGE` 设为 `reelucid:with-xhs`，然后运行 `docker compose up -d --no-build reelucid`。在设置中选择「容器内插件（试用）」。
- 抖音和 TikTok 可接 DTK。若 DTK 明确标记 TikTok 视频需要原会话，在设置中开启「允许 DTK 下载需要原会话的 TikTok 视频」。DTK 需启用 downloader 并有能访问该视频的会话，Reelucid 使用的 Key 需具备 `tiktok:read`、`media:write` 和 `media:read`。DTK 生成的媒体文件计入其自身存储。
- `GET /api/v1/source-platforms` 查询当前 yt-dlp 的提取器及上游维护标记。单条链接的可用性取决于访问权限、网络、登录状态和媒体格式。[历史平台实测](docs/source-validation/platform-audit-20261120.json) · [提取器补测](docs/source-validation/yt-dlp-live-20261123.json)
- 下载器在连接来源时检查 IP，拒绝私网、回环、元数据和保留地址。遇到重定向会重新检查。AI 使用独立客户端，管理员可连接宿主机上的 Ollama 等服务。
- 镜像固定 yt-dlp、EJS、Deno 版本，Deno 包装器禁网。下载限单条点播内容，受文件大小、时长和平台访问条件约束。
- 在网页中添加的 Netscape Cookie 会加密保存。任务通过选中的凭据按域名使用 Cookie，页面不会回显明文。
- 管理员可将自有适配器目录只读挂载并加入 `PYTHONPATH`，在 `REELUCID_ADAPTER_PLUGINS` 指定 `module:ClassName`（无参构造）。重启后在系统配置的 `adapters` 中启用对应 ID，通过 `source.adapter_id` 选择。[适配器接口](src/reelucid/sources.py)

原片会保留，便于播放和核对时间戳。设置 `keep_source=false` 会收到 422。任务成功后会清理用于转录的 PCM 文件。估算磁盘用量时，也要算上原片、产物、分块检查点和软删除记录。

## AI 设置与出处

在网页中设置 Provider（服务地址和凭据）、ModelBinding（模型）和 ProcessingProfile（处理预设）。兼容服务的 Base URL 通常以 `/v1` 结尾，并且需要支持 `chat/completions` 接口。Docker 中可用 `http://host.docker.internal:11434/v1` 访问宿主机的 Ollama，模型名填写 Ollama 中已安装的名称。

纯转录不会把文稿发给 AI 服务。摘要、翻译、提取和问答需要主动启用。使用远程服务时，还要在预设中允许远程 AI。模型返回 `text` 和形如 `S1` 的引用编号，后端校验编号并生成带原话和时间戳的 Evidence。回答是否符合原意，仍需对照原片判断。

API Key 用数据卷中的主密钥加密。备份时务必带上 `secrets/master_key`，恢复旧凭据需要这份密钥。当前是单管理员服务，不提供多租户隔离。

## 本地开发与 CLI

安装 uv 和 FFmpeg/ffprobe，并将它们加入 PATH。`.python-version` 固定了 Python 3.14.10。Node 版本见 `web/.node-version`。Python 和 Web 依赖分别由各自的锁文件固定。

```bash
uv sync --locked --extra server
uv run --locked --extra server reelucid models install small
uv run --locked --extra server reelucid transcribe demo.mp4 --model small --output output
cd web
npm ci
npm run build
cd ..
uv run --locked --extra server reelucid serve
```

开发网页时，可以运行 `npm run dev`，让 Vite 把请求转发给本地 API。Docker 镜像中的网页由 API 服务直接托管。Python wheel 提供转录核心、CLI 和 API。用 `uv run --locked --extra server reelucid doctor` 查看脱敏诊断信息，用 `uv run --locked --extra server reelucid init` 初始化本地目录并查看凭据文件的位置。

```bash
uv lock --check
uv run --locked --extra server ruff check src tests
uv run --locked --extra server ruff format --check src tests
uv run --locked --extra server pyright
uv run --locked --extra server pytest -m 'not model and not network and not gpu and not slow'
cd web && npm run check && npm run build
```

真实 ASR 测试需设置 `REELUCID_MODELS_DIR` 指向已安装 tiny 的模型根目录，`REELUCID_TEST_MEDIA` 指向含 meeting/report/budget 三句的测试语音，再执行 `pytest -m model`。本地协议 fixture 不等同于真实 LLM 的质量验收。

发行包可用 `uv build` 生成到 `dist/`。修改 HTTP 接口后，从项目根目录运行 CI 的 `Export OpenAPI` 命令更新契约文件。[CI 工作流](.github/workflows/ci.yml)

## 部署设置与回滚

| 环境变量 | 默认值/作用 |
|---|---|
| `REELUCID_IMAGE` | Compose 镜像标签，默认 `reelucid:1.1.0` |
| `REELUCID_SOURCE_PROXY` | 可选 HTTP CONNECT 代理。Docker Desktop 连接宿主 Clash 的示例为 `http://host.docker.internal:7890` |
| `REELUCID_DATA_DIR` | 本地默认为 `.data`，镜像使用 `/data` |
| `REELUCID_MODELS_DIR` | 数据目录下 `models` |
| `REELUCID_ADMIN_TOKEN` | 可选注入，至少 24 字符；未设置则创建 token 文件 |
| `REELUCID_ADMIN_TOKEN_FILE` / `REELUCID_MASTER_KEY_FILE` | 默认 `secrets/admin_token` / `secrets/master_key` |
| `REELUCID_MAX_UPLOAD_BYTES` | 默认硬上限 2 GiB，实际取此值与 Web 上传限额中较小的值 |
| `REELUCID_ASR_CHUNK_STRATEGY` | 默认 `auto`，明确选择英文的 `small` 任务使用相邻语音合并，其余在静音边界切分。可指定 `adjacent` 或 `silence` |
| `REELUCID_CPU_LIMIT` / `REELUCID_MEMORY_LIMIT` | Compose 硬上限，默认 `4.0` / `6g` |
| `REELUCID_MAX_CONCURRENT_JOBS` | 并发上限，默认 `4`，范围 `1`–`16`。设为 `1` 可固定串行 |
| `REELUCID_OFFLINE` | `true` 禁止 URL/模型下载与远程 AI；本地兼容 AI 仍可使用 |
| `REELUCID_ADAPTER_PLUGINS` | 可信 `module:Class` 入口，逗号分隔 |
| `TMPDIR` | 镜像使用 `/data/work/tmp`，大文件不占 `/tmp` 内存盘 |

下载、转录、转码和文本 AI 各有自己的并发额度，转录和转码共享 CPU 预算。同一素材的任务依次处理。无法探测容量时，调度器按 1 核、2 GiB 估算。默认 Compose 分配 4 核、6 GiB，资源充足时可并行跑两项 `small` 转录。CPU 使用率达到 80% 后，调度器暂停领取新任务。内存预留取 20% 和 384 MiB 两者中较高的值。用管理员 Token 请求 `GET /api/v1/capabilities`，可查看 `scheduler`、模型、代理和适配器状态。

单个媒体默认最长 4 小时，可在「模型与设置 → 资源限制」调整。`compose.yaml` 列出了 Compose 读取的项目级变量。应用运行时变量可在 Compose 服务的 `environment` 中设置。[默认 Compose 配置](compose.yaml)

将 `REELUCID_SOURCE_PROXY` 写入项目根目录的 `.env`，再运行 `docker compose up -d --no-deps reelucid` 应用设置。代理用于视频来源网页及媒体请求，外部解析服务 API、ASR 权重下载和文本 AI 各自使用独立客户端。

升级时沿用原 Compose 项目名和配置。等任务结束后停掉主服务，再备份完整数据卷，并记下旧镜像的标签或摘要。数据卷中包括 SQLite、WAL、密钥、配置、权重、原片和产物。

```bash
docker compose build reelucid
docker compose up -d --no-deps --wait reelucid
curl -fsS http://127.0.0.1:8000/api/v1/health/ready
```

新版本启动后，打开一条历史任务，并用短片试一次转录。回滚时先停止新版写入，再用原镜像和配置启动。如需恢复数据卷，先备份当前数据，因为旧快照只包含备份时刻的数据。日常停机可用 `docker compose down`，数据卷会保留。

### 常见问题

| 现象或错误 | 处理方式 |
| --- | --- |
| 长时间排队、`RESOURCE_LIMIT` | 在 `capabilities.scheduler` 查看 CPU、内存和阶段额度。等待宿主负载下降，或调整容器资源上限和模型大小。 |
| `SOURCE_ACCESS_REQUIRED`、`SOURCE_NOT_FOUND` | 检查作品能否访问并确认链接完整。需要会话时，在 Web 选择对应的站点凭据，或配置适用的可选组件。 |
| `NETWORK_POLICY_BLOCKED` | 确认链接指向公网 HTTP/HTTPS，使用 80 或 443 端口，并检查来源域名的 DNS 结果。 |
| `NO_AUDIO_TRACK` | 确认原片有音轨，或上传包含音轨的本地媒体。 |
| `NO_VIDEO_TRACK` | MP4 转码须使用含视频轨的原片。音频素材可选 MP3 或 WAV。 |
| HTTP 413、`STORAGE_EXHAUSTED` | 检查上传体积上限和数据卷剩余空间。 |

## 第三方许可

Reelucid 原创代码采用 Apache-2.0。第三方软件和模型适用各自的上游许可。[项目许可](LICENSE)

- Python 依赖清单见 `uv.lock`，原始许可随安装包保存在镜像的 `/app/.venv/lib/python3.14/site-packages/*.dist-info`。前端依赖清单见 `web/package-lock.json`。
- 预置 Whisper `small` 权重采用 MIT 许可。仓库、revision 和校验值记录在模型目录的 `reelucid-model.json`。[固定版本模型](https://huggingface.co/Systran/faster-whisper-small/tree/536b0662742c02347bc0e980a01041f333bce120)
- 镜像使用 Debian 启用 GPL 组件的 FFmpeg。版权和源码信息见镜像内 `/usr/share/doc/ffmpeg/copyright`，实际版本见 `/usr/local/share/reelucid-system-versions.txt`。PyAV wheel 内附 FFmpeg 库的许可声明随 wheel 提供。
- 可选 `with-xhs` 镜像包含 GPL-3.0 的 XHS-Downloader 2.8，对应源码和许可见 `/opt/reelucid-xhs/xhs-2.8-source.tar.gz`。同目录的 `resolve` 和 `resolve.py` 以 GPL-3.0-only 提供源码，构建配方和校验值见 Dockerfile。重新分发镜像时请保留对应源码、桥接脚本和第三方许可文件。[上游源码](https://github.com/JoeanAmier/XHS-Downloader/tree/47840a1bee8438324ff10753c4291148c46071c8) · [构建配方](Dockerfile)
