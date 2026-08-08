# Security Policy

## Supported versions

安全修复仅应用于最新发布版本：

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Older releases | No |

## Reporting a vulnerability

请通过 GitHub 的 [Private vulnerability reporting](https://github.com/fortressme/proxy2openconnect/security/advisories/new) 私密报告安全问题。请勿创建公开 Issue，也不要在报告中粘贴真实密码、OTP、证书私钥、会话 Cookie、VPN 地址、企业域名或未脱敏日志。

报告中建议包含：

- 受影响版本和部署方式；
- 可重复的最小步骤；
- 影响范围和可能的缓解方式；
- 已脱敏的日志或配置片段。

维护者会尽快确认报告、评估影响并协调修复与披露。在修复发布前，请避免公开漏洞细节。

## Deployment baseline

- 默认仅绑定 `127.0.0.1`，不要直接将管理端或代理端口暴露到公网。
- 使用独立的强随机 `ADMIN_PASSWORD` 和至少 32 字符的 `SESSION_SECRET`。
- 对外提供 Web 管理端时必须使用 HTTPS，并设置 `COOKIE_SECURE=true`。
- 首次启动后立即替换示例 Xray 代理密码。
- 优先使用企业 CA；使用自签名证书时，确认来源后再固定公钥指纹。
- 将 `data/vpn/config.json`、客户端证书和私钥作为密钥材料保护。
- 容器只需要 `/dev/net/tun` 与 `NET_ADMIN`，不应授予 `privileged`。
- 默认是 fail-open：VPN 断开后，新代理连接可能经普通网络发出。需要防泄漏边界时，请在宿主机或上层防火墙实施 fail-closed 策略。

## Sensitive data

`data/`、`.env`、日志和故障现场文件不应提交到版本库。`.gitignore` 与 `.dockerignore` 只能降低误提交概率，提交和发布前仍应主动复查暂存区与发布包。
