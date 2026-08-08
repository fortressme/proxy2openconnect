# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

## [0.1.0] - 2026-08-08

首个可发布版本。

### Added

- Xray SOCKS5/HTTP 入站与 Web JSON 配置管理。
- OpenConnect Cisco AnyConnect 兼容出口、密码/MFA、客户端证书与认证组配置。
- 基于 `SO_MARK` 和独立策略路由表的 IPv4/IPv6 fail-closed 出口。
- TUN 地址、MTU、接口状态和断线清理流程。
- 自签名服务器证书 TOFU 检测、管理员确认与公钥指纹固定。
- Web 登录、失败限流、安全响应头、内存日志和健康检查。
- Windows AnyConnect 4.10.08029 默认身份兼容配置。
- Docker Compose 持久化、端口覆盖和日志轮转配置。

### Security

- VPN 密码通过标准输入交给 OpenConnect，不出现在进程命令行中。
- 证书、私钥路径只允许位于 `/data/`。
- OpenConnect 额外参数采用安全允许列表。

