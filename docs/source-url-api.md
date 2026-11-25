# 来源 URL 查询 API（1.1.2）

基础路径为 `/api/v1`。以下为 Bash 示例，请替换演示 URL 为有权访问的音视频地址；`example.com` 不是可用的媒体测试源。不要在生产日志中记录含签名的地址或管理员 Token。

```bash
BASE_URL=http://127.0.0.1:8000
REELUCID_TOKEN="$(docker compose exec -T reelucid cat /data/secrets/admin_token)"

# 创建 URL 转码任务（202），无需加载语音模型。
# 记录返回的 asset_id 与 job_id；查询来源使用 asset_id。
curl -fsS -H "Authorization: Bearer $REELUCID_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: source-demo-001' \
  -d '{"source":{"kind":"url","url":"https://example.com/video.mp4?access=demo","adapter_id":"http_media"},"operations":[{"id":"media","type":"transcode","options":{"preset":"audio_mp3"}}]}' \
  "$BASE_URL/api/v1/jobs"

# 以实际任务返回的 ID 替换两个占位值。
ASSET_ID=ast_example
JOB_ID=job_example

# 200：仅管理员可查询；-i 同时展示 Cache-Control 响应头。
curl -fsSi -H "Authorization: Bearer $REELUCID_TOKEN" \
  "$BASE_URL/api/v1/assets/$ASSET_ID/source-url"

# 无认证应返回 401（不能使用含有效会话 Cookie 的客户端）。
curl -sS -o /dev/null -w '%{http_code}\n' \
  "$BASE_URL/api/v1/assets/$ASSET_ID/source-url"

# 普通素材详情：不返回完整原始链接。
curl -fsS -H "Authorization: Bearer $REELUCID_TOKEN" \
  "$BASE_URL/api/v1/assets/$ASSET_ID"

# 任务详情；任务进入终态后也可读取 result。
curl -fsS -H "Authorization: Bearer $REELUCID_TOKEN" \
  "$BASE_URL/api/v1/jobs/$JOB_ID"
curl -fsS -H "Authorization: Bearer $REELUCID_TOKEN" \
  "$BASE_URL/api/v1/jobs/$JOB_ID/result"
unset REELUCID_TOKEN
```

成功查询示例：

```http
HTTP/1.1 200 OK
Cache-Control: private, no-store
Content-Type: application/json

{"url":"https://example.com/video.mp4?access=demo"}
```

| 情况 | 查询接口状态 |
|---|---|
| URL 素材且已保存来源 | 200 |
| 无有效管理员认证 | 401 |
| 素材不存在、上传素材或无保存的 URL | 404 |

已认证的浏览器会话亦可访问。返回的是入库前解析/规范化后的来源链接，而非下载后的重定向地址，也不是完整分享文案。该接口不发起下载、不要求任务成功，也不绕过网络限制。

## 来源服务器返回 HTTP 412

任务提交成功仍为 HTTP 202。若下载阶段收到来源 HTTP 412，任务会进入失败状态。任务详情（HTTP 200）的相关字段示例：

```json
{
  "status": "failed",
  "error": {
    "code": "SOURCE_PRECONDITION_FAILED",
    "message": "来源服务拒绝了当前请求（HTTP 412）。",
    "retryable": false
  }
}
```

上述是字段摘录，不是完整任务响应。`retryable=false` 是错误分类的重试提示，不等于 HTTP 412 来自 Reelucid API，也不承诺服务器会自动重试。调用端应保留错误码，检查来源地址、有效期与访问条件后再决定是否手动重试。错误本身不会返回上游响应正文或完整签名 URL。

## 候选镜像验收

从仓库根目录在 Linux amd64 / Docker Compose v2 环境（Python 3.12+，端口 8000 空闲）执行：

```bash
python scripts/check_release_version.py --tag v1.1.2
docker buildx build --platform linux/amd64 --load -t reelucid:1.1.2-candidate .
bash scripts/smoke_release_image.sh reelucid:1.1.2-candidate never
```

脚本创建独立 Compose 项目和数据卷，使用仅用于测试的 HTTP CONNECT fixture，模拟真实 HTTP 412 响应而不访问互联网；随后重建 Reelucid 容器并检查原来的素材、任务和管理员 Token 是否仍有效。最终运行原有转码及语音转录验收，并清理测试资源。不要使用生产数据卷执行此测试；不要把 fixture 配置用于生产。
