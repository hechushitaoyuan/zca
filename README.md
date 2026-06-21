# zca

本项目由 Codex 基于 [liu5269/zcode2api](https://github.com/liu5269/zcode2api) 的 Python
网关、账号管理和 Web 后台，整合 [TriDefender/zcode-api](https://github.com/TriDefender/zcode-api)
的 Start Plan JWT 与阿里云验证码求解技术。

在上游基础上，本项目新增了免费 JWT 多账号公平轮询、单账号并发限制、额度均衡分担、
3012 风控账号指数冷却、验证码 single-flight 缓存、失败状态区分和真正的 SSE 流式转发。

## 快速开始

```bash
pip install -r requirements.txt
# 无痕验证求解器（无浏览器，Node + jsdom）。需已安装 Node.js：
cd captcha_node && npm install && cd ..
cp .env.example .env                     # 按需修改

python main.py serve                     # 启动网关 + 后台 UI（默认端口 3000）
```

- 后台管理：`http://localhost:3000/admin`（默认密码 `zcode`）
- 对话端点：`http://localhost:3000/v1/messages`（兼容 Anthropic Messages 协议）

## Docker 部署

镜像内同时包含 Python 网关与 Node/jsdom 验证码求解器。推荐使用仓库中的
`compose.yaml` 拉取预构建镜像：

```bash
cp .env.example .env
# 编辑 .env：设置 ZCA_IMAGE、后台密码和网关 API Key
docker compose pull
docker compose up -d
```

- 数据卷：容器内 `/data` 存放 `accounts.db`，由 `./data:/data` 持久化。
- 详细部署、健康检查与回滚步骤见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。
- **请勿**将 `.env`、`data/` 打入镜像——已在 `.dockerignore` 中排除。

### 自动构建镜像（Docker Hub 多架构）

`.github/workflows/docker-build.yml` 在 push 到 `main`、打 `v*` tag 或手动触发时，
先跑 E2 测试（`compileall` + `unittest` + `node --check`），全绿后构建
**`linux/amd64` + `linux/arm64`** 多架构镜像并推送到 **Docker Hub**（镜像名 `<用户名>/zca`）。
Pull Request 仅测试 + 构建验证、**不推送**。

- 登录凭据仅来自仓库 Secrets：`DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`，不写入仓库文件。
- 标签：默认分支 `latest`、短 SHA、`v*` tag。

```bash
# 拉取并运行已发布镜像（tag: latest 或 sha-xxxxxxx）
docker run -d --name zca -p 8047:3000 \
  -v "$(pwd)/data:/data" -e ZCODE_ADMIN_KEY=<强密码> \
  <你的 Docker Hub 用户名>/zca:latest
```

### VPS 部署（/opt/zca，实验端口 8047）

使用预构建镜像 + `compose.yaml` 部署到 VPS 实验端口 8047（与既有 8046 服务并存，互不影响）。
完整步骤、健康检查、版本固定与回滚见 **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**。

## 后台 UI

| 页面 | 说明 |
|------|------|
| `/admin/login` | 后台登录（Bearer 密钥鉴权，凭证加密存于浏览器 localStorage）|
| `/admin/accounts` | 账号池：新增/导入/导出、启用禁用、**实时额度与状态监控**（每 5 秒刷新）|
| `/admin/settings` | 后台密码、网关 API Key |

账号池页实时展示每个账号的状态（正常 / 额度用完 / 限流 / 异常 / 禁用）、各模型剩余额度、
调用与失败次数。请求按 round-robin 分发，**某账号额度用完会自动切换到下一个账号**，并在 UI 中即时反映。

## 多账号轮询与换号

- 在「账号池」粘贴 Start Plan JWT（3 段点分）或 Coding Plan API Key，每行一个即可加入轮询。
- 网关每次请求选择下一个「可用」账号（跳过用完 / 限流 / 异常 / 禁用）。
- 命中额度用完信号（余额为 0、上游 402、错误体含 quota/余额 等）→ 标记 `exhausted` 并换下一个账号。
- 上游 429 → 标记 `cooling` 冷却一段时间后自动恢复；401/403（非验证码）→ 标记 `invalid`。
- 后台任务按 `ZCODE_QUOTA_REFRESH_INTERVAL` 周期刷新各账号额度；也可在 UI 手动刷新。

## 鉴权

- **后台鉴权**：所有 `/admin/api/*` 需 `Authorization: Bearer <后台密码>`。
- **网关鉴权（可选）**：在「设置」配置「网关 API Key」后，`/v1/messages` 须携带
  `Authorization: Bearer <key>` 或 `x-api-key: <key>`；留空则不校验。

## 无痕验证（无浏览器）

Start Plan（JWT）模式调用 `zcode.z.ai` 上游时需要阿里云无痕验证参数
（请求头 `X-Aliyun-Captcha-Verify-Param`）。本项目**不启动任何真实浏览器**，
而是用 **Node + jsdom** 在模拟浏览器环境中运行阿里云官方无痕 SDK 来求得该参数。

- 求解器位于 `captcha_node/solver.js`；首次使用前需执行 `cd captcha_node && npm install`。
- `app/captcha.py` 以子进程方式调用求解器，内置结果缓存（默认 45s）、并发去重与失败重试。
- 求解器在 jsdom 中补齐了 SDK 依赖的浏览器 API（`matchMedia`、canvas/WebGL、`Worker`、`OffscreenCanvas`），
  执行 `startTracelessVerification` 后输出 `verifyParam`。
- `verifyParam` 实为 `base64(JSON{certifyId, sceneId, isSign, securityToken})`，由阿里云服务端签发。
- 仅 Coding Plan（JWT）账号需要；API Key 账号走 `api.z.ai` 回退端点，无需验证码。

> 求解器运行的是阿里云自家混淆 SDK。若阿里云更新其指纹逻辑（feilin / cloudauth-device），
> 可能需要相应调整 `solver.js` 中补齐的浏览器 API 桩。该方案无需真实浏览器，比无头 Chromium 轻量很多。

## 命令行

```bash
python main.py serve [--port 3000]                 # 启动服务
python main.py login zai [--no-browser]            # OAuth 登录 Z.AI 并自动入池
python main.py add-account zai <name> <jwt|key>    # 添加轮询账号
python main.py accounts [zai|bigmodel]             # 查看账号列表
python main.py remove-account <provider> <id|name> # 删除账号
python main.py quota                               # 查看各账号实时额度
python main.py status                              # 查看配置概览
python main.py set-admin-key <key>                 # 设置后台密码
python main.py export [file] / import <file>       # 导出 / 导入账号
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ZCODE_PORT` | 3000 | 服务端口 |
| `ZCODE_HOST` | 0.0.0.0 | 监听地址 |
| `ZCODE_ADMIN_KEY` | zcode | 后台密码初始值（之后以 DB 为准）|
| `ZCODE_GATEWAY_KEY` | 空 | `/v1/messages` 网关访问密钥 |
| `ZCA_BIND_IP` | 127.0.0.1 | Compose 宿主机监听地址；公网直连时显式设为 0.0.0.0 |
| `ZCODE_DATA_DIR` | ./data | 数据目录（SQLite 存放处）|
| `ZCODE_QUOTA_REFRESH_INTERVAL` | 60 | 后台刷新额度间隔（秒），0 关闭 |
| `ZCODE_COOLING_SECONDS` | 300 | 限流冷却时长（秒）|
| `ZCODE_NODE_PATH` | node | 无痕验证求解器使用的 Node 可执行文件 |
| `ZCODE_CAPTCHA_TIMEOUT` | 40 | 单次验证码求解超时（秒）|
| `ZCODE_CAPTCHA_RETRIES` | 4 | 验证码求解失败重试次数 |
| `CAPTCHA_CACHE_TTL` | 45000 | 验证码缓存时长 (ms) |
| `ZAI_UPSTREAM_URL` / `ZAI_FALLBACK_URL` / `BIGMODEL_UPSTREAM_URL` | — | 上游端点 |

## 项目结构

```
├── app/
│   ├── main.py            # FastAPI 应用工厂 + 生命周期
│   ├── settings.py        # 环境变量 / 配置
│   ├── models.py          # Account 数据模型与状态
│   ├── store.py           # SQLite 持久化 + 轮询游标（data/accounts.db）
│   ├── agent.py           # 上游请求构建
│   ├── captcha.py         # 无痕验证求解（调用 Node 求解器）
│   ├── quota.py           # 额度查询 + 后台用量监控
│   ├── oauth.py           # Z.AI OAuth 登录流程
│   ├── auth_admin.py      # 后台 / 网关鉴权
│   ├── logs.py            # 彩色终端日志
│   ├── routes/            # gateway / admin_api / pages
│   └── statics/           # app.css, auth.js, toast.js, header.js, admin/*.html
├── captcha_node/          # 无浏览器无痕验证求解器（Node + jsdom，solver.js）
├── main.py                # 命令行入口（serve / login / accounts / quota ...）
├── data/                  # 运行时生成：accounts.db (SQLite)
├── Dockerfile             # 镜像（Python + Node）
├── compose.yaml           # 预构建镜像部署
├── .dockerignore
├── .github/workflows/     # 测试及 Docker Hub 多架构构建
├── docs/ARCHITECTURE.md   # 架构概览
├── requirements.txt
└── .env.example
```

## 技术栈

- Python 3.13 · FastAPI · Uvicorn · httpx
- SQLite（账号 / 设置持久化，WAL 模式）
- Node.js + jsdom（无浏览器求解阿里云无痕验证 → verifyParam）

## 文档

- [架构概览 docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 系统架构图、请求流程、账号状态机、无痕验证流程与已知限制。

## 致谢

- [liu5269/zcode2api](https://github.com/liu5269/zcode2api)：本项目的 Python 主体、管理界面与账号池基础。
- [TriDefender/zcode-api](https://github.com/TriDefender/zcode-api)：Start Plan JWT 与阿里云验证码求解技术来源。
- UI 设计参考：[chenyme/grok2api](https://github.com/chenyme/grok2api)。

## 许可证

本项目采用 [AGPL-3.0](LICENSE) 许可证。

## 重要免责声明

本项目纯属技术学习和个人自用测试。开发目的主要是统一管理本人拥有或获授权使用的
ZCode 账号，避免在 ZCode 客户端中频繁手动切换账号，并通过 ZCode 客户端进行个人测试。

本项目不提供任何形式的商业授权、适用性保证、可用性保证或结果保证，也不保证免费额度、
验证码方案或第三方接口能够持续工作。

作者及仓库维护者不对因使用、修改、分发、部署或依赖本项目而产生的任何直接或间接损失、账号封禁、数据丢失、法律风险或第三方索赔负责。

请勿将本项目用于违反服务条款、协议、法律法规或平台规则的场景。商业使用前请自行确认 LICENSE、相关协议以及你是否获得了作者的书面许可。
