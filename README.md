# zca

本项目由 Codex 基于 [liu5269/zcode2api](https://github.com/liu5269/zcode2api) 的 Python
网关、账号管理和 Web 后台，整合 [TriDefender/zcode-api](https://github.com/TriDefender/zcode-api)
的 Start Plan JWT 与阿里云验证码求解技术。

在上游基础上，本项目由Codex指导新增了免费 JWT 多账号公平轮询、单账号并发限制、额度均衡分担、
3012 风控账号指数冷却、验证码 single-flight 缓存、失败状态区分和真正的 SSE 流式转发。

## ## 致谢

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
