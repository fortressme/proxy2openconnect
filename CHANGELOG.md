# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/) 和 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 的基本结构。

## [Unreleased]

- 增加项目 Logo、浏览器 favicon、Apple Touch/Web App 图标，并将品牌标识应用到登录页、控制台侧栏和 README。
- 增加 VPN 会话退出后的自动重连（最多连续 5 次），以及可在 Web 控制台配置 HTTP(S) 地址和间隔的网址保活。
- 增加全局 DNS 来源选择、手动 DNS 服务器配置、系统解析器恢复和 DNS 专用 VPN 路由。
- 增加反向代理公网来源白名单，修复 HTTPS 终止代理后的写请求来源校验。
- 增加运行总览统计、实时流量趋势、活动目标地址，以及按 VPN 配置和日期持久化的目标连接频次历史与保留周期设置；目标统计优先展示原始域名并附解析 IP。

## [0.1.0] - 2026-08-08

首个公开版本。

### Added

- Xray SOCKS5/HTTP 入站与 Web 配置管理。
- OpenConnect Cisco AnyConnect 兼容出口，支持密码、MFA、客户端证书和认证组。
- `all`、`vpn`、`manual` 三种路由模式：默认接管全部，也可采用 VPN 下发的 split routes 或手动 CIDR 列表。
- 基于 `SO_MARK`、Xray outbound 标签和独立策略路由表的 IPv4/IPv6 VPN 出口。
- TUN 地址、MTU、接口状态、重连与断线清理。
- 自签名服务器证书 TOFU 检测、管理员确认与公钥指纹固定。
- Web 登录、失败限流、安全响应头、内存日志和健康检查。
- Docker Compose 部署、持久化、端口覆盖和日志轮转。
- 多架构容器镜像发布与持续集成检查。

### Security

- VPN 密码通过标准输入传递给 OpenConnect，不出现在进程命令行中。
- 证书和私钥路径限制在 `/data/` 内。
- OpenConnect 额外参数使用安全允许列表。
- 默认仅监听本机地址，管理会话使用独立签名密钥。

### Known limitations

- 当前只支持单个 VPN 会话。
- 默认断线策略为 fail-open，不构成防泄漏边界。
- VPN 下发路由的解析依赖 OpenConnect 输出格式，升级相关组件后应重新验证。

[Unreleased]: https://github.com/fortressme/proxy2openconnect/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/fortressme/proxy2openconnect/releases/tag/v0.1.0
