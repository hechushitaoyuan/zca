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

日志脱敏检查：确认日志中 **不出现** 完整 JWT、API Key、verifyParam、后台密码等敏感串。
`/health` 与 `/meta` 仅返回 `status/version/commit`，不含账号、配置或凭据。

## 3.1 公网直连（可选，需先满足安全前置）

仅当甲方明确要求通过公网 IP（如 `http://<公网IP>:8047/admin`）访问时，才放开监听地址。

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
