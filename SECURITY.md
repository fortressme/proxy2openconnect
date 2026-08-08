# Security Policy

## Supported versions

当前仅维护最新发布版本。发现安全问题后，请先停止公开暴露管理端和代理端口，并在私密渠道联系项目维护者；不要在公开 Issue 中提交密码、OTP、VPN 地址、证书私钥、完整日志或公司网络信息。

## Deployment baseline

- 默认仅绑定 `127.0.0.1`，不要直接暴露到公网。
- 生产环境必须使用独立强随机 `ADMIN_PASSWORD` 和至少 32 字符的 `SESSION_SECRET`。
- 对外提供 Web 管理端时必须使用 HTTPS，并设置 `COOKIE_SECURE=true`。
- 首次启动后立即替换默认 Xray 代理密码。
- 仅在确认来源后固定自签名证书指纹；优先使用企业 CA。
- 将 `data/vpn/config.json`、客户端证书和私钥按密钥材料保护。
- 容器只需要 `/dev/net/tun` 与 `NET_ADMIN`，不应授予 `privileged`。

## Sensitive data

`data/`、`.env`、运行日志和测试现场信息不应提交到版本库。仓库已经通过 `.gitignore` 和 `.dockerignore` 排除这些内容，但发布者仍应在发布前执行 README 中的敏感信息扫描。

