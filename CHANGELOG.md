# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

## [0.1.0] - 2026-08-08

首个可发布版本。

### Added

- Xray SOCKS5/HTTP 入站与 Web JSON 配置管理。
- OpenConnect Cisco AnyConnect 兼容出口、密码/MFA、客户端证书与认证组配置。
- 基于 `SO_MARK`、outbound 标签和独立策略路由表的 IPv4/IPv6 VPN 出口。
- TUN 地址、MTU、接口状态和断线清理流程。
- 自签名服务器证书 TOFU 检测、管理员确认与公钥指纹固定。
- Web 登录、失败限流、安全响应头、内存日志和健康检查。
- Windows AnyConnect 4.10.08029 默认身份兼容配置。
- Docker Compose 持久化、端口覆盖和日志轮转配置。

### Changed

- 只为 `XRAY_VPN_OUTBOUND_TAGS` 选中的 Xray outbound 注入 VPN mark，默认标签为 `vpn-out`。
- VPN 未连接或退出时撤销策略规则并回落到普通默认路由，不再安装 `unreachable` 路由。
- 策略表仅安装 OpenConnect 下发的 IPv4/IPv6 split-include 路由，并以 split-exclude 路由覆盖排除网段；不再自行创建 VPN 默认路由。

### Security

- VPN 密码通过标准输入交给 OpenConnect，不出现在进程命令行中。
- 证书、私钥路径只允许位于 `/data/`。
- OpenConnect 额外参数采用安全允许列表。

