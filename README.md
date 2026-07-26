# Aymo2API

将 [aymo.ai](https://aymo.ai/) 的 better-auth 会话、每日额度和临时对话数据流包装为
OpenAI Chat Completions 与 Anthropic Messages 兼容接口。项目结构沿用 notto2api 的
账号池、轮询、管理面板、工具调用和注册任务能力，注册使用 aymo.ai 的
`signup` + 邮箱验证 + Cloudflare Turnstile 流程。

## 功能

- `POST /v1/chat/completions`：OpenAI 兼容，支持流式/非流式、多轮消息、工具调用协议。
- `POST /v1/messages`：Anthropic Messages 兼容，支持 `x-api-key`、流式事件和工具调用。
- `GET /v1/models`、`GET /v1/models/{model_id}`：aymo.ai 模型与常用别名。
- 账号池：Round-Robin、每日额度预扣、better-auth 会话校验、Cookie 持久化、定时探测。
- 管理面板：`/admin/dashboard`，支持添加、刷新、启用、禁用、删除和批量操作。
- 注册任务：`/admin/register/start`，走 better-auth 邮箱验证 + 本地 Turnstile solver。

## 工作原理

aymo.ai 前端把后端代理在 `https://aymo.ai/backend-api`（即 `https://backend.aymo.ai`），
鉴权使用 **better-auth**：

- 会话是滚动的不透明 Cookie `__Secure-better-auth.session_token`（不是 JWT，无刷新端点）。
- 注册/登录都需要 Cloudflare Turnstile，站点密钥 `0x4AAAAAACgaobmeaCdcpmsb`，
  通过请求头 `x-captcha-response` 提交。
- 对话使用网页端“临时对话”接口
  `POST /api/organizations/{orgId}/messages/temporary`，请求体
  `{content, modelId, history, enableReasoning, enableWebSearch}`，返回
  `event: chunk / done / error` 的 SSE 流。项目把它归一化成 OpenAI/Anthropic 响应。
- 额度来自 `/backend-api/users/me/today-usage`（`dailyCreditLimit - creditsUsed`），
  每日重置。

## 快速启动
### turnstile solver
需要部署一个本地运行的 **[Turnstile solver](https://github.com/D3-vin/Turnstile-Solver-NEW)**（默认 `http://127.0.0.1:5072`，GET
`/turnstile?url=&sitekey=` → `{taskId}`，GET `/result?id=` → `{solution:{token}}`）；
或者设置 `YESCAPTCHA_KEY` 走云端打码。
### temp-mail
部署[temp-mail](https://temp-mail-docs.awsl.uk/zh/)      
配置.env中
```.env
WORKER_DOMAIN=你的worker地址，如xxxx.xxxx.workers.dev
EMAIL_DOMAIN=邮箱域名
ADMIN_PASSWORD=设置的管理员密钥
```


```powershell
Copy-Item .env.example .env
# 编辑 .env：至少填写 API_KEY（生产环境）、邮箱 worker、Turnstile solver
pip install -r requirements.txt
python main.py
```

默认监听 `http://127.0.0.1:8019`（可通过 `PORT` 修改）。管理面板地址为
`http://127.0.0.1:8019/admin/dashboard`。

Docker：

```bash
docker compose up -d --build
```

账号文件挂载在 `data/accounts.json`。

## 账号文件

`data/accounts.json` 是数组。注册任务会自动写入这些字段；手动导入时，
只填 `email` + `password` 也可以（探测阶段会自动登录，需要 Turnstile solver），
或直接提供 `session_token`（`__Secure-better-auth.session_token` 的值）跳过登录。

```json
[
  {
    "email": "USER@example.com",
    "password": "PASSWORD",
    "user_id": "uuid",
    "org_id": "uuid",
    "session_token": "SESSION_COOKIE_VALUE",
    "cookies": {
      "__Secure-better-auth.session_token": "SESSION_COOKIE_VALUE"
    },
    "inbox_token": "MAILBOX_JWT",
    "disabled": false
  }
]
```

会话过期后无法直接刷新，服务会用保存的邮箱和密码重新调用 `sign-in/email`
（需要一次新的 Turnstile 解算）。

## OpenAI 示例

```bash
curl http://localhost:8019/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "stream": true,
    "messages": [{"role":"user","content":"hello"}]
  }'
```

免费账号可用模型（`GET /v1/models` 里 `free_tier: true`）：`gpt-5-nano`、
`gemini-2.5-flash-lite`、`gemma-4-31b-it`、`deepseek/deepseek-v4-flash`、
`qwen/qwen3-next-80b-a3b-thinking`、`mistralai/mistral-nemo`、
`meta-llama/llama-4-scout-17b-16e-instruct` 等，以及让 aymo 自动选择的 `auto`。
付费模型（`gpt-5`、`claude-opus-4-8` 等）需要对应订阅的账号。常用别名：
`gpt-4o`、`claude-sonnet`、`claude-opus`、`deepseek-chat`、`deepseek-r1`、
`kimi-k3`、`grok`、`glm`、`qwen-max`。

## Anthropic 示例（含 Claude Code CLI）

```bash
curl http://localhost:8019/v1/messages \
  -H "x-api-key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5-nano",
    "max_tokens": 256,
    "messages": [{"role":"user","content":"hello"}]
  }'
```

## 工具调用

上游只有文本数据流，项目将 OpenAI/Anthropic `tools` 声明编码为工具协议提示词，
并把 `@@TOOL_CALL_BEGIN@@ ... @@TOOL_CALL_END@@` 解析成标准 `tool_calls` /
`tool_use` 内容块（逻辑与 notto2api 一致）。

## 注册

在 `.env` 填写邮箱 worker 与 solver：

```dotenv
WORKER_DOMAIN=newmail.example.workers.dev
EMAIL_DOMAIN=example.com
ADMIN_PASSWORD=MAILBOX_ADMIN_PASSWORD
TURNSTILE_SOLVER_URL=http://127.0.0.1:5072
```

启动任务：

```bash
curl -X POST http://localhost:8019/admin/register/start \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"count":1}'

curl http://localhost:8019/admin/register/status \
  -H "Authorization: Bearer YOUR_API_KEY"

curl -X POST http://localhost:8019/admin/register/stop \
  -H "Authorization: Bearer YOUR_API_KEY"
```

注册流程：创建临时邮箱 → 解算 Turnstile → `sign-up/email` → 轮询邮箱拿到
`verify-email` 链接 → 后端校验并自动登录 → 读取 org 与额度 → 入池。
可选在请求体传 `captcha_token` 复用一次性令牌。

## 管理接口

| 接口 | 作用 |
|---|---|
| `GET /admin/tokens` | 账号总数、存活数、总剩余额度 |
| `GET /admin/tokens/detail` | 脱敏账号详情 |
| `POST /admin/tokens/add` | 批量导入账号 |
| `POST /admin/tokens/reload` | 热加载 `accounts.json` |
| `POST /admin/tokens/refresh` | 全量校验会话并刷新每日额度 |
| `POST /admin/tokens/{id}/refresh` | 刷新单个账号 |
| `POST /admin/tokens/bulk/*` | 批量刷新/启用/禁用/删除 |

`API_KEY` 留空时为本地开发模式；部署到公网时建议设置强随机值。

## 环境变量

完整模板见 `.env.example`。最常用配置：

- `TOKEN_FILE`：账号文件路径，默认 `data/accounts.json`
- `KEEP_ALIVE_MINUTES`：定时会话/额度探测间隔
- `CHAT_MAX_RETRIES`：上游失败重试次数
- `AYMO_DEFAULT_MODEL`：未识别模型时的回退（默认 `gpt-5-nano`）
- `TURNSTILE_SOLVER_URL` / `YESCAPTCHA_KEY`：Turnstile 解算
- `PROXY`、`PROXY_LIST`：请求与注册代理
- `SHARE_ENABLED`、`SHARE_URL`：注册成功后同步到其他实例

## 已知限制

- 走的是“临时对话”接口，不落库、不支持图片附件（传入的图片会被忽略）。
- 免费账号有每日额度上限；额度耗尽的账号会被跳过，次日重置后自动恢复。
- 会话刷新依赖 Turnstile solver；solver 不可用时过期账号无法自动重登。

## 开发检查

```bash
python -m compileall app main.py
```
