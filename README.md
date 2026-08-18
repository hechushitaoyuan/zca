# zca

本项目由 Codex 基于 [liu5269/zcode2api](https://github.com/liu5269/zcode2api) 的 Python
网关、账号管理和 Web 后台，整合 [TriDefender/zcode-api](https://github.com/TriDefender/zcode-api)
的 Start Plan JWT 与阿里云验证码求解技术。

在上游基础上，本项目由Codex指导新增了免费 JWT 多账号公平轮询、单账号并发限制、额度均衡分担、
3012 风控账号指数冷却、一次性验证码串行求解、失败状态区分和真正的 SSE 流式转发。

## 当前兼容性

- ZCode 客户端身份与 Start Plan 请求已同步到 3.7.7。
- Dockerfile 原生支持 `linux/amd64` 与 `linux/arm64`，验证码由真实 Chromium 运行官方 SDK。
- 风控要求点选或滑块时，可在账号池生成一次性“本地验证”链接，复制到 Windows
  Chrome/Edge 完成人工验证；结果仅在内存短暂保存并供下一次请求消费一次。详见
  [部署文档](docs/DEPLOYMENT.md#31-交互式验证码仅风控触发时)。

## 访问与管理

- 访问服务根地址（例如 `http://127.0.0.1:8047/`）即可打开后台登录页；旧的
  `/admin/login` 地址仍保留兼容。
- 模型客户端的 Base URL 可填写 `http://127.0.0.1:8047/v1`，实际对话端点为
  Anthropic Messages 协议的 `POST /v1/messages`。
- 设置页提供流量日志，只保存状态、模型、路径、账号名称、Token 用量和耗时等
  运行元数据，不保存提示词、模型输出、验证码或凭据。
- 账号池支持选择 `GLM-5.3`、`GLM-5.2` 或 `GLM-5-Turbo`，并对指定账号发起最小调用测试。
- 账号池“本地验证”可生成随机、短期链接；Windows 浏览器完成验证后应立即重试模型。
  每条验证结果只能供一个模型请求使用，过期、消费后均不可复用，也不会写入 SQLite。
- “授权登录”同步官方 ZCode 3.7.7 authorization-code 流程：显示 Z.ai 官方认证网址，
  可复制到本地 Windows 无痕浏览器或直接打开；也可直接粘贴 Windows ZCode/浏览器已生成、
  尚未消费的官方回调网址。后台保存 Coding Plan JWT、OAuth token 响应及用户资料；一次性
  回调 code 不保存，本地浏览器 Cookie 无法被服务端读取。

## 致谢

- [liu5269/zcode2api](https://github.com/liu5269/zcode2api)：本项目的 Python 主体、管理界面与账号池基础。
- [TriDefender/zcode-api](https://github.com/TriDefender/zcode-api)：Start Plan JWT 与阿里云验证码求解技术来源。
- UI 设计参考：[chenyme/grok2api](https://github.com/chenyme/grok2api)。

## 许可证

本项目采用 [AGPL-3.0](LICENSE) 许可证。

## 重要免责声明

本人不是做IT的，只是单纯对AI感兴趣的给排水工程师：

本项目纯属技术学习和个人自用测试。开发目的主要是统一管理本人拥有或获授权使用的
ZCode 账号，避免在 ZCode 客户端中频繁手动切换账号，并通过 ZCode 客户端进行个人测试。

本项目不提供任何形式的商业授权、适用性保证、可用性保证或结果保证，也不保证免费额度、
验证码方案或第三方接口能够持续工作。

作者及仓库维护者不对因使用、修改、分发、部署或依赖本项目而产生的任何直接或间接损失、账号封禁、数据丢失、法律风险或第三方索赔负责。

请勿将本项目用于违反服务条款、协议、法律法规或平台规则的场景。商业使用前请自行确认 LICENSE、相关协议以及你是否获得了作者的书面许可。
