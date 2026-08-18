# zca 部署与回滚（实验环境 /opt/zca）

本文档用于把预构建的多架构镜像部署到 VPS 的 **实验端口 8047**，与既有 **8046** 服务并存。

> 重要边界：本流程 **不停止、不修改 8046 上的任何既有服务**。8047 是独立实验实例，使用独立目录 `/opt/zca` 与独立数据卷。

## 0. 前置条件

- VPS 已安装 Docker 与 Docker Compose 插件（`docker compose version` 可用）。
- 镜像已由 GitHub Actions 构建并推送到 Docker Hub（`linux/amd64` + `linux/arm64`）。
- 已知道要部署的镜像引用：`your-dockerhub-user/zca:latest` 或某个固定 `:sha-xxxxxxx`。

## 1. 首次创建目录与配置

```bash
sudo mkdir -p /opt/zca/data
cd /opt/zca

# 从仓库取得 compose.yaml 与 .env.example（scp / git archive / 手动复制均可），放到 /opt/zca 下
cp .env.example .env
```

编辑 `.env`，至少设置：

```ini
# 要部署的镜像（建议固定到 sha tag 以便精确回滚）
ZCA_IMAGE=your-dockerhub-user/zca:sha-xxxxxxx
# 必须改成强密码，切勿沿用默认 zcode
ZCODE_ADMIN_KEY=<强密码>
```

> `.env` 含后台密码，仅存在于 VPS 本地，不要提交到仓库（`.gitignore` 已忽略 `.env`）。

## 2. 拉取并启动

```bash
cd /opt/zca
docker compose pull
docker compose up -d
```

- `image: ${ZCA_IMAGE:?...}`：未设置 `ZCA_IMAGE` 会直接报错并拒绝启动，避免误用本地 build。
- compose **不在 VPS 上构建镜像**，只拉取预构建镜像。

## 3. 健康与日志检查

```bash
# 容器状态应为 healthy（镜像内 HEALTHCHECK 探活 /health）
docker compose ps

# 宿主机侧探活（端口仅绑定回环 127.0.0.1:8047 → 容器 3000）
curl -fsS http://127.0.0.1:8047/health
# 期望：{"status":"ok","version":"...","commit":"..."}

# 查看日志，确认无堆栈异常
docker compose logs --tail=100 zca
```

> 默认端口绑定为 `127.0.0.1:8047`（由 `.env` 的 `ZCA_BIND_IP`，缺省 `127.0.0.1`），
> **仅本机可访问**，实验服务不直接暴露到公网。本机 Cloudflare Tunnel / SSH 转发验证不受影响。

### 3.1 交互式验证码（仅风控触发时）

镜像默认在 ARM64/AMD64 容器内运行真实 Chromium。无痕验证通过时无需操作；若上游要求
点选/滑块，容器求解器会快速释放账号并提示使用 Windows 本地浏览器验证，避免单个请求
占用账号数分钟。

1. 登录后台账号池，点击右上角“本地验证”。
2. 复制一次性链接到自己的 Windows Chrome/Edge，或直接点击“打开”。
3. 在正常浏览器窗口完成人工滑块；页面显示“验证完成”后立即回到 ZCode 重试模型。
4. 每条结果只能消费一次。如果 ZCode 同时探测两个模型，需要重新生成并再验证一次。

链接默认有效 10 分钟，完成后的结果默认只等待 120 秒，分别可通过
`ZCODE_CAPTCHA_BROWSER_LINK_TTL`、`ZCODE_CAPTCHA_BROWSER_RESULT_TTL` 调整。随机链接本身是
短期访问凭据，应只发给本人；`verifyParam` 只在服务进程内存中保存，不写数据库、日志或 API
响应，消费或过期后立即清除。

阿里云可能把验证结果与浏览器网络环境绑定。因此 Windows 直连生成的结果能否被 VPS 请求接受，
最终以上游返回为准；本功能提供真实人工验证链路，不绕过或伪造验证。如果直连结果被拒绝，
可让 Windows 浏览器通过该 VPS 的受控代理出口再打开同一链接，使验证与模型请求出口一致。

容器 noVNC 仍可用于诊断 Chromium 页面，但不再作为人工滑块的主要流程。若启用 6080，只能绑定
回环或明确的 Tailscale IP，禁止使用 `0.0.0.0`：

```ini
ZCA_NOVNC_BIND_IP=<VPS 的 Tailscale IPv4>
ZCA_NOVNC_PORT=6080
```

重新执行 `docker compose up -d` 后，同一 Tailnet 内可查看诊断桌面；访问范围由 Tailscale ACL 控制。

### 3.2 Z.ai 网页授权登录

后台账号池的“授权登录”使用当前 ZCode 3.7.7 authorization-code 流程。由本地 Windows
ZCode/浏览器生成且尚未被客户端消费的官方回调网址可以直接粘贴导入，不要求先在本项目
生成链接。如果还没有回调网址，也可点击“生成新的认证链接”，直接打开或复制到 Windows
无痕浏览器完成登录。OAuth 不使用 VPS noVNC，也不会占用验证码桌面。

官方流程最终通过 `https://zcode.z.ai/app/oauth/login` 桥接到
`zcode://oauth/callback`。这个自定义协议属于用户本地安装的 ZCode，VPS 无法自动接收；因此
认证完成后若浏览器询问是否打开 ZCode，应先取消，再复制地址栏里的完整官方桥接网址，粘贴
回管理面板并点击“验证并导入”。对于本项目生成的流程，后台会额外核对本次随机 `state`；
对于外部 ZCode 流程，则采用回调中的 `state` 完成官方 token 兑换。数据库保存最终 ZCode
JWT、OAuth token 响应（含上游实际返回的 access/refresh token 等字段）及用户资料。已消费的
一次性授权码和完整回调网址不保存；本地浏览器 Cookie 不会发送给 VPS，因此无法保存。
认证链接默认保留 10 分钟，可用 `ZCODE_OAUTH_TIMEOUT` 调整。

日志脱敏检查：确认日志中 **不出现** 完整 JWT、API Key、verifyParam、后台密码等敏感串。
`/health` 与 `/meta` 仅返回 `status/version/commit`，不含账号、配置或凭据。

## 3.3 公网直连（可选，需先满足安全前置）

仅当明确要求通过公网 IP（如 `http://<公网IP>:8047/`）访问时，才放开监听地址。

**放开前必须全部满足：**

1. `.env` 设置 **非空强** `ZCODE_ADMIN_KEY`（后台登录），切勿沿用默认 `zcode`。
2. `.env` 设置 **非空强随机** `ZCODE_GATEWAY_KEY`（`/v1/messages` 鉴权）。
   - 新库会用该值初始化；既有库若当前为空会被补齐；**已在后台设置的非空值不会被覆盖**。
   - 若数据库网关 Key 仍为空就公网放开，等于网关无鉴权，属高风险。
3. 确认云厂商安全组 / 防火墙 **仅放行预期来源** 到 8047，且 **未误开** 8046 等其他端口。

满足后再设置：

```ini
# .env
ZCA_BIND_IP=0.0.0.0
ZCODE_ADMIN_KEY=<强密码>
ZCODE_GATEWAY_KEY=<强随机值>
```

```bash
docker compose up -d   # 重新应用端口绑定
# 本机自检（不经公网）
curl -fsS http://127.0.0.1:8047/health
```

放开后可直接访问：

- 后台登录：`http://<公网IP>:8047/`
- API Base URL：`http://<公网IP>:8047/v1`
- Anthropic Messages：`http://<公网IP>:8047/v1/messages`

通过 Cloudflare Tunnel 接入域名时，源站填写 `http://127.0.0.1:8047`，随后同样使用
`https://你的域名/` 登录、使用 `https://你的域名/v1` 作为客户端 Base URL。Cloudflare
只代理 8047；noVNC 的 6080 仍须保持回环地址并经 SSH 隧道访问。

> 回退到仅本机：把 `ZCA_BIND_IP` 改回 `127.0.0.1`（或删除该行）后 `docker compose up -d`。
> 切勿在日志、报告或提交中回显真实 `ZCODE_ADMIN_KEY` / `ZCODE_GATEWAY_KEY`。

## 4. 版本固定与回滚

通过 **镜像 SHA tag** 固定版本，回滚只切换镜像标签，**不删除数据卷或数据库**。

```bash
# 回滚：把 .env 中 ZCA_IMAGE 改回上一个已知良好的 sha tag
#   ZCA_IMAGE=your-dockerhub-user/zca:sha-<上一个版本>
docker compose pull
docker compose up -d
```

- 数据在宿主机 `./data`（容器内 `/data`），跨版本升级/回滚均保留。
- **禁止** 用 `docker compose down -v` 或删除 `/opt/zca/data` 的方式“回滚”——那会清空账号库。

## 5. 与 8046 既有服务的关系

- 8047 实例使用独立的 `container_name: zca`、独立目录 `/opt/zca`、独立卷 `/opt/zca/data`。
- 部署、重启、回滚 8047 **均不触碰** 8046 的容器、端口、数据或配置。
- 切勿在 8047 的 compose 中复用 8046 的数据目录或端口。

## 6. 私有镜像仓库（仅在需要时）

仅当 Docker Hub 仓库为 **私有** 时，VPS 才需登录后才能 `pull`：

```bash
docker login -u <dockerhub 用户名>
# 按提示输入 Access Token（不要写进 compose.yaml、.env 或本文档）
```

- 公开仓库无需 `docker login`。
- Docker Hub Token 属凭据，**只在交互式 `docker login` 时输入**，绝不写入 `compose.yaml`、`.env` 或任何文档/仓库文件。
